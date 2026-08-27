"""Web entrypoint for public, refreshable Hidalgo survey maps.

This server is intentionally separate from the MCP transport. It provides a
small browser workflow: upload an Excel/CSV or paste a public Google Sheets
URL, generate one aggregate map, and share a stable /maps/<id> URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analytics import generate_dominant_answer_map, inspect_dataset  # noqa: E402
from app.hosted_maps import HostedMapStore, PublicSheetError, download_public_sheet, iso_now  # noqa: E402
from app.osm import get_road_lines  # noqa: E402


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_TERRAIN_BYTES = 8 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xls", ".csv"}
store = HostedMapStore()


def _safe_json(value: Any) -> str:
    """Serialize JSON for embedding inside a script tag."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return configured or f"{request.url.scheme}://{request.url.netloc}"


def _public_page(envelope: dict[str, Any], request: Request) -> str:
    template = (ROOT / "ui" / "map.html").read_text(encoding="utf-8")
    marker = "<script>\n(() => {"
    if marker not in template:
        raise RuntimeError("No encontré el punto de integración de la interfaz del mapa.")
    map_id = str(envelope["map_id"])
    version_url = f"{_base_url(request)}/api/maps/{map_id}/version"
    terrain_url = f"{_base_url(request)}/maps/{map_id}/terrain.jpg"
    osm_roads_url = f"{_base_url(request)}/api/maps/{map_id}/osm-roads"
    config = {
        "toolOutput": envelope["result"],
        "publicMap": True,
        "publicMapId": map_id,
        "publicMapVersion": envelope["version"],
        "publicMapVersionUrl": version_url,
        "publicMapTerrainUrl": terrain_url,
        "publicMapOsmRoadsUrl": osm_roads_url,
        "publicMapPollMs": 30000,
    }
    bootstrap = f"<script>window.openai={_safe_json(config)};</script>\n<script>\n(() => {{"
    return template.replace(marker, bootstrap, 1)


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _question_names(inspection: dict[str, Any]) -> list[str]:
    questions = inspection.get("schema", {}).get("questions", [])
    return [
        item.get("name") if isinstance(item, dict) else str(item)
        for item in questions
        if (item.get("name") if isinstance(item, dict) else str(item))
    ]


def _coordinates(value: Any, output: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    if output is None:
        output = []
    if isinstance(value, list) and len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        output.append((float(value[0]), float(value[1])))
    elif isinstance(value, list):
        for item in value:
            _coordinates(item, output)
    return output


def _terrain_path(map_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,32}", map_id):
        raise ValueError("El identificador del mapa no es válido.")
    destination = store.root / "terrain" / f"{map_id}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _ensure_terrain(envelope: dict[str, Any]) -> Path | None:
    destination = _terrain_path(str(envelope["map_id"]))
    if destination.exists() and destination.stat().st_size:
        return destination
    result = envelope.get("result", {})
    features = result.get("background", {}).get("features", []) or result.get("territory_background", {}).get("features", [])
    points = [point for feature in features for point in _coordinates(feature.get("geometry", {}).get("coordinates"))]
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    padding = .015
    query = urlencode({
        "bbox": f"{min(longitudes) - padding},{min(latitudes) - padding},{max(longitudes) + padding},{max(latitudes) + padding}",
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": "1400,1000",
        "format": "jpg",
        "f": "image",
        "transparent": "false",
    })
    url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/export?{query}"
    try:
        with urlopen(UrlRequest(url, headers={"User-Agent": "MapaParticipacionCiudadana/0.1"}), timeout=30) as response:  # nosec B310 - fixed public basemap host
            content = response.read(MAX_TERRAIN_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(content) > MAX_TERRAIN_BYTES or not content_type.startswith("image/"):
            return None
        temporary = destination.with_suffix(".jpg.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination
    except Exception:
        return None


async def home(_: Request) -> HTMLResponse:
    return HTMLResponse((ROOT / "ui" / "hosted.html").read_text(encoding="utf-8"))


async def inspect_public_sheet(request: Request) -> JSONResponse:
    try:
        form = await request.form()
        sheet_url = str(form.get("sheet_url") or "").strip()
        if not sheet_url:
            raise ValueError("Pega una URL pública de Google Sheets para detectar sus opciones.")
        source_path, _ = download_public_sheet(sheet_url, store.source_cache)
        inspection = inspect_dataset(file_path=str(source_path))
        return JSONResponse(
            {
                "municipalities": inspection.get("municipalities", []),
                "questions": _question_names(inspection),
                "records": inspection.get("records", 0),
                "source_name": inspection.get("source_name"),
                "warnings": inspection.get("warnings", [])[:5],
            },
            headers={"Cache-Control": "no-store"},
        )
    except (PublicSheetError, ValueError) as error:
        return _error(str(error))
    except Exception as error:
        return _error(f"No pude leer la hoja: {error}")


async def create_map(request: Request) -> JSONResponse:
    try:
        form = await request.form()
        sheet_url = str(form.get("sheet_url") or "").strip()
        municipality = str(form.get("municipality") or "").strip() or None
        question = str(form.get("question") or "").strip() or None
        upload = form.get("file")

        if sheet_url:
            source_path, content_hash = download_public_sheet(sheet_url, store.source_cache)
            source_type = "google_sheets"
            source_label = "Google Sheets público"
        elif isinstance(upload, UploadFile) and upload.filename:
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise ValueError("El archivo debe ser .xlsx, .xls o .csv.")
            source_path = store.source_cache / f"upload-{secrets.token_hex(12)}{suffix}"
            total = 0
            with source_path.open("wb") as destination:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        source_path.unlink(missing_ok=True)
                        raise ValueError("El archivo supera el límite de 50 MB.")
                    destination.write(chunk)
            if total == 0:
                source_path.unlink(missing_ok=True)
                raise ValueError("El archivo está vacío.")
            content_hash = None
            source_type = "file"
            source_label = upload.filename
        else:
            raise ValueError("Pega una URL pública de Google Sheets o carga un Excel/CSV.")

        inspection = inspect_dataset(file_path=str(source_path))
        municipalities = inspection.get("municipalities", [])
        questions = inspection.get("schema", {}).get("questions", [])
        if not municipality:
            if len(municipalities) == 1:
                municipality = municipalities[0]
            elif len(municipalities) > 1:
                raise ValueError("La hoja contiene varios municipios. Indica cuál quieres representar.")
        if not question:
            if len(questions) == 1:
                question = questions[0].get("name") if isinstance(questions[0], dict) else str(questions[0])
            elif len(questions) > 1:
                raise ValueError("La hoja contiene varias preguntas. Indica cuál quieres representar.")

        result = generate_dominant_answer_map(
            file_path=str(source_path),
            municipality=municipality,
            question=question,
        )
        source = {
            "type": source_type,
            "label": source_label,
            "municipality": result.get("municipality") or municipality,
            "question": result.get("question") or question,
            "created_at": iso_now(),
        }
        if source_type == "google_sheets":
            source.update({
                "url": sheet_url,
                "content_hash": content_hash,
                "checked_at": iso_now(),
                "last_error": None,
            })
        envelope = store.create(result, source)
        _ensure_terrain(envelope)
        map_url = f"{_base_url(request)}/maps/{envelope['map_id']}"
        return JSONResponse({
            "map_id": envelope["map_id"],
            "map_url": map_url,
            "version": envelope["version"],
            "source_type": source_type,
            "municipality": result.get("municipality"),
            "question": result.get("question"),
        })
    except (PublicSheetError, ValueError) as error:
        return _error(str(error))
    except Exception as error:  # keep client errors readable without exposing a traceback
        return _error(f"No pude generar el mapa: {error}")


async def public_map(request: Request) -> HTMLResponse | JSONResponse:
    try:
        envelope = store.refresh_if_due(request.path_params["map_id"])
        _ensure_terrain(envelope)
        return HTMLResponse(_public_page(envelope, request), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def map_version(request: Request) -> JSONResponse:
    try:
        envelope = store.refresh_if_due(request.path_params["map_id"])
        source = envelope.get("source", {})
        return JSONResponse(
            {
                "map_id": envelope["map_id"],
                "version": envelope["version"],
                "updated_at": envelope["updated_at"],
                "source_type": source.get("type"),
                "last_error": source.get("last_error"),
            },
            headers={"Cache-Control": "no-store"},
        )
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def terrain(request: Request) -> FileResponse | JSONResponse:
    try:
        envelope = store.get(request.path_params["map_id"])
        path = _ensure_terrain(envelope)
        if not path:
            return _error("No hay relieve disponible para este mapa.", 404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def osm_roads(request: Request) -> JSONResponse:
    try:
        envelope = store.get(request.path_params["map_id"])
        result = envelope.get("result", {})
        features = result.get("background", {}).get("features", []) or result.get("territory_background", {}).get("features", [])
        roads = get_road_lines(features, store.root / "osm-cache")
        return JSONResponse(roads, headers={"Cache-Control": "public, max-age=86400"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


routes = [
    Route("/", home, methods=["GET"]),
    Route("/api/sheets/inspect", inspect_public_sheet, methods=["POST"]),
    Route("/api/maps", create_map, methods=["POST"]),
    Route("/api/maps/{map_id}/version", map_version, methods=["GET"]),
    Route("/maps/{map_id}/terrain.jpg", terrain, methods=["GET"]),
    Route("/api/maps/{map_id}/osm-roads", osm_roads, methods=["GET"]),
    Route("/maps/{map_id}", public_map, methods=["GET"]),
    Mount("/assets", app=StaticFiles(directory=ROOT / "ui" / "assets"), name="assets"),
]

app = Starlette(debug=False, routes=routes)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Servidor de mapas públicos de participación ciudadana")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8770")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
