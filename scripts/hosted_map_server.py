"""Web entrypoint for public, refreshable Hidalgo survey maps.

This server is intentionally separate from the MCP transport. It provides a
small browser workflow: upload an Excel/CSV or paste a public Google Sheets
URL, generate one aggregate map, and share a stable /maps/<id>-<municipality>
URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from html import escape as escape_xml
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analytics import generate_dominant_answer_map, inspect_dataset  # noqa: E402
from app.hosted_maps import MAP_ID_PATTERN, HostedMapStore, PublicSheetError, download_public_workbook, iso_now  # noqa: E402
from app.osm import get_road_lines  # noqa: E402


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_TERRAIN_BYTES = 8 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xls", ".csv"}
store = HostedMapStore()


def _require_durable_storage() -> None:
    if (os.getenv("VERCEL") or os.getenv("VERCEL_ENV")) and not store.uses_persistent_storage:
        raise ValueError(
            "El servidor está en Vercel, pero no tiene almacenamiento persistente. "
            "Conecta un almacén Vercel Blob al proyecto y configura BLOB_READ_WRITE_TOKEN."
        )


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


def _municipality_slug(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "Hidalgo")).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:48] or "hidalgo"


def _map_reference(map_id: str, municipality: str | None) -> str:
    if not MAP_ID_PATTERN.fullmatch(map_id):
        raise ValueError("El identificador del mapa no es válido.")
    return f"{map_id}-{_municipality_slug(municipality)}"


def _resolve_map_id(map_reference: str) -> str:
    candidate = str(map_reference or "").split("-", 1)[0]
    if not MAP_ID_PATTERN.fullmatch(candidate):
        raise ValueError("El identificador del mapa no es válido.")
    return candidate


def _public_page(envelope: dict[str, Any], request: Request) -> str:
    template = (ROOT / "ui" / "map.html").read_text(encoding="utf-8")
    marker = "<script>\n(() => {"
    if marker not in template:
        raise RuntimeError("No encontré el punto de integración de la interfaz del mapa.")
    map_id = str(envelope["map_id"])
    information_url = f"{_base_url(request)}/informacion"
    version_url = f"{_base_url(request)}/api/maps/{map_id}/version"
    terrain_url = f"{_base_url(request)}/maps/{map_id}/terrain.jpg"
    osm_roads_url = f"{_base_url(request)}/api/maps/{map_id}/osm-roads"
    result = envelope.get("result", {})
    background_features = result.get("background", {}).get("features", []) or result.get("territory_background", {}).get("features", [])
    bbox = _bbox_parameter(background_features)
    if bbox:
        osm_roads_url = f"{osm_roads_url}?{urlencode({'bbox': bbox})}"
    config = {
        "toolOutput": envelope["result"],
        "publicMap": True,
        "publicMapId": map_id,
        "publicMapVersion": envelope["version"],
        "publicMapVersionUrl": version_url,
        "publicInformationUrl": information_url,
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


def _bbox_parameter(features: list[dict[str, Any]]) -> str | None:
    points = [point for feature in features for point in _coordinates(feature.get("geometry", {}).get("coordinates"))]
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return ",".join(f"{value:.7f}" for value in (min(latitudes), min(longitudes), max(latitudes), max(longitudes)))


def _osm_key(map_id: str) -> str:
    if not MAP_ID_PATTERN.fullmatch(map_id):
        raise ValueError("El identificador del mapa no es válido.")
    return f"osm/{map_id}.json"


def _map_background_features(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("background", {}).get("features", []) or result.get("territory_background", {}).get("features", [])


def _fetch_osm_context(result: dict[str, Any]) -> dict[str, Any]:
    """Fetch OSM once while hosting a map; later visits use the stored artifact."""
    return get_road_lines(_map_background_features(result), store.root / "osm-cache")


PREVIEW_COLORS = ["#7b1e3a", "#1f5a4a", "#b38b59", "#4b6682", "#9c5a4f", "#3b806d", "#866b46", "#5a7182"]


def _preview_rings(geometry: dict[str, Any] | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if kind == "Polygon":
        return [ring for ring in coordinates if isinstance(ring, list) and len(ring) >= 3]
    if kind == "MultiPolygon":
        return [
            ring
            for polygon in coordinates
            if isinstance(polygon, list)
            for ring in polygon
            if isinstance(ring, list) and len(ring) >= 3
        ]
    if kind == "GeometryCollection":
        rings: list[list[list[float]]] = []
        for child in geometry.get("geometries", []):
            rings.extend(_preview_rings(child))
        return rings
    return []


def _preview_projection(result: dict[str, Any]):
    feature_groups = [
        result.get("background", {}).get("features", []),
        result.get("territory_background", {}).get("features", []),
        result.get("feature_collection", {}).get("features", []),
    ]
    points = [
        point
        for group in feature_groups
        for feature in group
        for point in _coordinates(feature.get("geometry", {}).get("coordinates"))
    ]
    if not points:
        points = [
            (float(site["longitude"]), float(site["latitude"]))
            for site in result.get("influence_sites", [])
            if site.get("longitude") is not None and site.get("latitude") is not None
        ]
    if not points:
        return lambda point: (500.0, 350.0)
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    span = max(max_x - min_x, max_y - min_y, 0.001)
    padding = span * 0.06
    min_x -= padding
    max_x += padding
    min_y -= padding
    max_y += padding

    def project(point: tuple[float, float]) -> tuple[float, float]:
        x = (point[0] - min_x) / max(max_x - min_x, 1e-9) * 1000
        y = (max_y - point[1]) / max(max_y - min_y, 1e-9) * 700
        return round(x, 2), round(y, 2)

    return project


def _preview_path(geometry: dict[str, Any] | None, project) -> str:
    paths: list[str] = []
    for ring in _preview_rings(geometry):
        points = []
        for point in ring:
            if isinstance(point, list) and len(point) >= 2:
                try:
                    points.append(project((float(point[0]), float(point[1]))))
                except (TypeError, ValueError):
                    continue
        if len(points) >= 3:
            commands = [f"M {points[0][0]} {points[0][1]}"]
            commands.extend(f"L {x} {y}" for x, y in points[1:])
            commands.append("Z")
            paths.append(" ".join(commands))
    return " ".join(paths)


def _preview_anchor(feature: dict[str, Any], project) -> tuple[float, float] | None:
    points = _coordinates(feature.get("geometry", {}).get("coordinates"))
    if not points:
        return None
    longitude = sum(point[0] for point in points) / len(points)
    latitude = sum(point[1] for point in points) / len(points)
    return project((longitude, latitude))


def _preview_color(properties: dict[str, Any], categories: list[str]) -> str:
    answer = str(properties.get("dominant_answer") or "")
    if not answer and properties.get("answer_counts"):
        answer = max(properties["answer_counts"].items(), key=lambda item: int(item[1]))[0]
    try:
        index = categories.index(answer)
    except ValueError:
        index = 0
    return PREVIEW_COLORS[index % len(PREVIEW_COLORS)] if answer else "#dfeae2"


def _preview_svg(envelope: dict[str, Any]) -> str:
    result = envelope.get("result", {})
    project = _preview_projection(result)
    categories = [str(category) for category in result.get("response_categories", [])]
    background = result.get("background", {}).get("features", [])
    territory = result.get("territory_background", {}).get("features", [])
    data_features = result.get("feature_collection", {}).get("features", [])
    paths: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 700" role="img" aria-labelledby="preview-title preview-description">',
        '<title id="preview-title">Vista previa cartográfica</title>',
        f'<desc id="preview-description">Mapa de {escape_xml(str(result.get("municipality") or "Hidalgo"))} con respuesta predominante por localidad.</desc>',
        '<rect width="1000" height="700" fill="#edf2ef"/>',
    ]
    for feature in background:
        path = _preview_path(feature.get("geometry"), project)
        if path:
            paths.append(f'<path d="{path}" fill="#f7f8f7" stroke="#547364" stroke-width="2.2"/>')
    for feature in territory:
        path = _preview_path(feature.get("geometry"), project)
        if path:
            paths.append(f'<path d="{path}" fill="#dfeae2" fill-opacity=".55" stroke="#9aafa4" stroke-width=".65"/>')
    labels: list[tuple[float, float, str, str]] = []
    for feature in data_features:
        properties = feature.get("properties", {})
        path = _preview_path(feature.get("geometry"), project)
        if not path:
            continue
        color = _preview_color(properties, categories)
        paths.append(f'<path d="{path}" fill="{color}" fill-opacity=".78" stroke="#ffffff" stroke-width="1.15"/>')
        anchor = _preview_anchor(feature, project)
        if anchor:
            labels.append((anchor[0], anchor[1], str(properties.get("locality") or ""), color))
    for site in result.get("influence_sites", []):
        if site.get("longitude") is None or site.get("latitude") is None:
            continue
        x, y = project((float(site["longitude"]), float(site["latitude"])))
        color = _preview_color(site, categories)
        paths.append(f'<circle cx="{x}" cy="{y}" r="5.5" fill="{color}" stroke="#ffffff" stroke-width="1.5"/>')
    for x, y, label, _ in labels:
        if label:
            paths.append(
                f'<text x="{x + 7}" y="{y + 3}" fill="#252525" font-family="Montserrat, Arial, sans-serif" font-size="10" font-weight="600" paint-order="stroke" stroke="#ffffff" stroke-width="3">{escape_xml(label)}</text>'
            )
    title = escape_xml(str(result.get("municipality") or "Mapa de Hidalgo"))
    question = escape_xml(str(result.get("question") or "Respuesta predominante"))
    paths.extend([
        '<rect x="18" y="18" width="310" height="76" rx="4" fill="#ffffff" fill-opacity=".94" stroke="#d8d2c8"/>',
        f'<text x="32" y="45" fill="#611232" font-family="Montserrat, Arial, sans-serif" font-size="17" font-weight="700">{title}</text>',
        f'<text x="32" y="68" fill="#686868" font-family="Montserrat, Arial, sans-serif" font-size="11">{question}</text>',
    ])
    if categories:
        legend_height = 28 + len(categories) * 22
        x = 18
        y = 660 - legend_height
        paths.append(f'<rect x="{x}" y="{y}" width="310" height="{legend_height}" rx="4" fill="#ffffff" fill-opacity=".94" stroke="#d8d2c8"/>')
        paths.append(f'<text x="{x + 14}" y="{y + 20}" fill="#4b0d27" font-family="Montserrat, Arial, sans-serif" font-size="11" font-weight="700">Respuesta predominante</text>')
        for index, category in enumerate(categories):
            item_y = y + 39 + index * 22
            paths.append(f'<circle cx="{x + 20}" cy="{item_y - 3}" r="5" fill="{PREVIEW_COLORS[index % len(PREVIEW_COLORS)]}"/>')
            paths.append(f'<text x="{x + 34}" y="{item_y}" fill="#333333" font-family="Montserrat, Arial, sans-serif" font-size="10">{escape_xml(category)}</text>')
    paths.append('<text x="985" y="684" text-anchor="end" fill="#68766f" font-family="Montserrat, Arial, sans-serif" font-size="9">Vista previa · INEGI / Hidalgo</text>')
    paths.append('</svg>')
    return "".join(paths)


def _terrain_path(map_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,32}", map_id):
        raise ValueError("El identificador del mapa no es válido.")
    destination = store.root / "terrain" / f"{map_id}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _terrain_key(map_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,32}", map_id):
        raise ValueError("El identificador del mapa no es válido.")
    return f"terrain/{map_id}.jpg"


def _ensure_terrain(envelope: dict[str, Any]) -> Path | None:
    map_id = str(envelope["map_id"])
    destination = _terrain_path(map_id)
    if store.uses_persistent_storage and store.has_binary(_terrain_key(map_id)):
        return None
    if not store.uses_persistent_storage and destination.exists() and destination.stat().st_size:
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
        if store.uses_persistent_storage:
            store.write_binary(_terrain_key(map_id), content, "image/jpeg")
            return None
        temporary = destination.with_suffix(".jpg.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination
    except Exception:
        return None


async def home(_: Request) -> HTMLResponse:
    return HTMLResponse((ROOT / "ui" / "hosted.html").read_text(encoding="utf-8"))


async def information(_: Request) -> HTMLResponse:
    return HTMLResponse(
        (ROOT / "ui" / "informacion.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def inspect_public_sheet(request: Request) -> JSONResponse:
    try:
        form = await request.form()
        sheet_url = str(form.get("sheet_url") or "").strip()
        sheet_name = str(form.get("sheet_name") or "").strip() or None
        if not sheet_url:
            raise ValueError("Pega una URL pública de Google Sheets para detectar sus opciones.")
        source_path, _ = download_public_workbook(sheet_url, store.source_cache)
        workbook_inspection = inspect_dataset(file_path=str(source_path))
        sheets = workbook_inspection.get("sheets", [])
        inspection = inspect_dataset(file_path=str(source_path), sheet_name=sheet_name) if sheet_name else workbook_inspection
        if not sheet_name and len(sheets) > 1:
            return JSONResponse(
                {
                    "sheets": sheets,
                    "requires_sheet_selection": True,
                    "municipalities": [],
                    "questions": [],
                    "records": inspection.get("records", 0),
                    "source_name": inspection.get("source_name"),
                    "warnings": inspection.get("warnings", [])[:5],
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "sheets": sheets,
                "requires_sheet_selection": False,
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
        _require_durable_storage()
        form = await request.form()
        sheet_url = str(form.get("sheet_url") or "").strip()
        sheet_name = str(form.get("sheet_name") or "").strip() or None
        municipality = str(form.get("municipality") or "").strip() or None
        question = str(form.get("question") or "").strip() or None
        upload = form.get("file")

        if sheet_url:
            source_path, content_hash = download_public_workbook(sheet_url, store.source_cache)
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

        inspection = inspect_dataset(file_path=str(source_path), sheet_name=sheet_name)
        if source_type == "google_sheets" and not sheet_name:
            sheets = inspection.get("sheets", [])
            if len(sheets) == 1:
                sheet_name = sheets[0].get("name")
                inspection = inspect_dataset(file_path=str(source_path), sheet_name=sheet_name)
            elif len(sheets) > 1:
                raise ValueError("Elige la hoja de Google Sheets que quieres representar.")
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
            sheet_name=sheet_name,
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
                "sheet_name": sheet_name,
                "content_hash": content_hash,
                "checked_at": iso_now(),
                "last_error": None,
            })
        osm_context = _fetch_osm_context(result)
        envelope = store.create(result, source)
        try:
            store.write_json(_osm_key(str(envelope["map_id"])), osm_context)
        except Exception:
            # A map remains usable if the OSM artifact store is temporarily
            # unavailable; the compatibility endpoint can retry once later.
            pass
        map_reference = _map_reference(str(envelope["map_id"]), result.get("municipality"))
        map_url = f"{_base_url(request)}/maps/{map_reference}"
        preview_url = f"{_base_url(request)}/maps/{map_reference}/preview.svg"
        return JSONResponse({
            "map_id": envelope["map_id"],
            "map_url": map_url,
            "preview_url": preview_url,
            "version": envelope["version"],
            "source_type": source_type,
            "municipality": result.get("municipality"),
            "question": result.get("question"),
            "sheet_name": sheet_name,
        })
    except (PublicSheetError, ValueError) as error:
        return _error(str(error))
    except Exception as error:  # keep client errors readable without exposing a traceback
        return _error(f"No pude generar el mapa: {error}")


async def public_map(request: Request) -> HTMLResponse | JSONResponse:
    try:
        envelope = store.refresh_if_due(_resolve_map_id(request.path_params["map_id"]))
        return HTMLResponse(_public_page(envelope, request), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def map_version(request: Request) -> JSONResponse:
    try:
        envelope = store.refresh_if_due(_resolve_map_id(request.path_params["map_id"]))
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
        envelope = store.get(_resolve_map_id(request.path_params["map_id"]))
        if store.uses_persistent_storage:
            content = store.read_binary(_terrain_key(str(envelope["map_id"])))
            if content is None:
                _ensure_terrain(envelope)
                content = store.read_binary(_terrain_key(str(envelope["map_id"])))
            if not content:
                return _error("No hay relieve disponible para este mapa.", 404)
            return Response(content, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
        path = _ensure_terrain(envelope)
        if not path:
            return _error("No hay relieve disponible para este mapa.", 404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def preview(request: Request) -> Response | JSONResponse:
    try:
        envelope = store.get(_resolve_map_id(request.path_params["map_id"]))
        return Response(
            _preview_svg(envelope),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


async def osm_roads(request: Request) -> JSONResponse:
    try:
        map_id = _resolve_map_id(request.path_params["map_id"])
        stored = store.read_json(_osm_key(map_id))
        if stored and stored.get("type") == "FeatureCollection":
            return JSONResponse(stored, headers={"Cache-Control": "public, max-age=31536000, immutable"})

        envelope = store.get(map_id)
        bbox_text = str(request.query_params.get("bbox") or "").strip()
        if bbox_text:
            values = [float(value) for value in bbox_text.split(",")]
            if len(values) != 4 or not (values[0] < values[2] and values[1] < values[3]):
                raise ValueError("El límite OSM no es válido.")
            roads = get_road_lines([], store.root / "osm-cache", bbox=tuple(values))
        else:
            result = envelope.get("result", {})
            roads = _fetch_osm_context(result)
        try:
            store.write_json(_osm_key(map_id), roads)
        except Exception:
            pass
        return JSONResponse(roads, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    except FileNotFoundError:
        return _error("No encontré ese mapa público.", 404)
    except ValueError as error:
        return _error(str(error), 400)


routes = [
    Route("/", home, methods=["GET"]),
    Route("/informacion", information, methods=["GET"]),
    Route("/api/sheets/inspect", inspect_public_sheet, methods=["POST"]),
    Route("/api/maps", create_map, methods=["POST"]),
    Route("/api/maps/{map_id}/version", map_version, methods=["GET"]),
    Route("/maps/{map_id}/terrain.jpg", terrain, methods=["GET"]),
    Route("/maps/{map_id}/preview.svg", preview, methods=["GET"]),
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
