"""Public map snapshots and public Google Sheets ingestion.

This module deliberately keeps the source adapter separate from the existing
survey and geography engine. A hosted map stores only the aggregated result;
the uploaded workbook or downloaded sheet is never served from the public URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from app.analytics import generate_dominant_answer_map


MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
SHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


class PublicSheetError(ValueError):
    """Raised when a Google Sheets URL cannot be read safely as public data."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _result_version(result: dict[str, Any]) -> str:
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _map_id() -> str:
    return secrets.token_urlsafe(18).replace("-", "_")


def _sheet_export_url(public_url: str) -> str:
    parts = urlsplit(public_url.strip())
    if parts.scheme != "https" or parts.hostname != "docs.google.com" or parts.username or parts.password or parts.port:
        raise PublicSheetError("Usa un enlace HTTPS público de Google Sheets (docs.google.com).")
    match = SHEET_ID_PATTERN.search(parts.path)
    if not match:
        raise PublicSheetError("No pude identificar el ID de la hoja en el enlace proporcionado.")
    query = parse_qs(parts.query)
    gid = query.get("gid", [None])[0]
    if not gid and parts.fragment:
        fragment_query = parse_qs(parts.fragment.lstrip("#?"))
        gid = fragment_query.get("gid", [None])[0]
    parameters = {"format": "csv"}
    if gid and re.fullmatch(r"\d+", gid):
        parameters["gid"] = gid
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?{urlencode(parameters)}"


def download_public_sheet(public_url: str, cache_dir: str | Path) -> tuple[Path, str]:
    """Download one public sheet tab as CSV and return path plus content hash."""
    export_url = _sheet_export_url(public_url)
    request = Request(
        export_url,
        headers={"User-Agent": "MapaParticipacionCiudadana/0.1 (+public-sheet-import)"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310 - host is allow-listed above
            advertised_size = response.headers.get("Content-Length")
            try:
                if advertised_size and int(advertised_size) > MAX_SOURCE_BYTES:
                    raise PublicSheetError("La hoja pública supera el límite de 50 MB.")
            except ValueError as error:
                raise PublicSheetError("Google devolvió un tamaño de archivo inválido.") from error
            content = response.read(MAX_SOURCE_BYTES + 1)
    except HTTPError as error:
        raise PublicSheetError("Google no permitió leer la hoja. Confirma que cualquiera con el enlace tenga permiso de lectura.") from error
    except (URLError, TimeoutError) as error:
        raise PublicSheetError("No pude conectar con Google Sheets en este momento.") from error
    if len(content) > MAX_SOURCE_BYTES:
        raise PublicSheetError("La hoja pública supera el límite de 50 MB.")
    if content.lstrip().lower().startswith((b"<!doctype", b"<html", b"<head")):
        raise PublicSheetError("El enlace no devolvió datos CSV públicos. Publica la hoja o habilita lectura para cualquiera con el enlace.")
    if not content.strip():
        raise PublicSheetError("La hoja pública está vacía.")

    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    url_key = hashlib.sha256(export_url.encode("utf-8")).hexdigest()
    destination = cache / f"{url_key}.csv"
    destination.write_bytes(content)
    return destination, hashlib.sha256(content).hexdigest()


class HostedMapStore:
    """Small file-backed store suitable for local use and easy cloud migration."""

    def __init__(self, root: str | Path | None = None, refresh_seconds: int | None = None):
        project_root = Path(__file__).resolve().parents[1]
        # Vercel packages the project on a read-only filesystem.  Keep the
        # local file-backed store for development, but use its writable
        # scratch directory in serverless deployments.
        if root is None:
            if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
                root = Path("/tmp") / "mapa-participacion-ciudadana"  # noqa: S108 - Vercel's writable scratch space
            else:
                root = os.getenv("HOSTED_MAPS_DIR", "").strip() or project_root / "work" / "hosted-maps"
        self.root = Path(root)
        self.source_cache = self.root / "source-cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_cache.mkdir(parents=True, exist_ok=True)
        self.refresh_seconds = max(15, int(refresh_seconds or os.getenv("SHEET_REFRESH_SECONDS", "60")))
        self._lock = threading.Lock()

    def _path(self, map_id: str) -> Path:
        if not MAP_ID_PATTERN.fullmatch(map_id):
            raise ValueError("El identificador del mapa no es válido.")
        return self.root / f"{map_id}.json"

    def _write(self, envelope: dict[str, Any]) -> None:
        target = self._path(str(envelope["map_id"]))
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def get(self, map_id: str) -> dict[str, Any]:
        path = self._path(map_id)
        if not path.exists():
            raise FileNotFoundError("No encontré el mapa solicitado.")
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, result: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        map_id = _map_id()
        now = iso_now()
        envelope = {
            "map_id": map_id,
            "created_at": now,
            "updated_at": now,
            "version": _result_version(result),
            "source": source,
            "result": result,
        }
        self._write(envelope)
        return envelope

    def refresh_if_due(self, map_id: str) -> dict[str, Any]:
        """Refresh a Google Sheet snapshot when the configured interval elapsed."""
        envelope = self.get(map_id)
        source = envelope.get("source", {})
        if source.get("type") != "google_sheets":
            return envelope
        checked_at = _parse_iso(source.get("checked_at"))
        if checked_at and utc_now() - checked_at < timedelta(seconds=self.refresh_seconds):
            return envelope

        with self._lock:
            envelope = self.get(map_id)
            source = envelope.get("source", {})
            checked_at = _parse_iso(source.get("checked_at"))
            if checked_at and utc_now() - checked_at < timedelta(seconds=self.refresh_seconds):
                return envelope
            source["checked_at"] = iso_now()
            try:
                sheet_path, content_hash = download_public_sheet(source["url"], self.source_cache)
                if content_hash != source.get("content_hash"):
                    result = generate_dominant_answer_map(
                        file_path=str(sheet_path),
                        municipality=source.get("municipality") or None,
                        question=source.get("question") or None,
                    )
                    envelope["result"] = result
                    envelope["version"] = _result_version(result)
                    envelope["updated_at"] = iso_now()
                    source["content_hash"] = content_hash
                    source["last_error"] = None
                else:
                    source["last_error"] = None
            except Exception as error:  # keep the last known good map available
                source["last_error"] = str(error)
            envelope["source"] = source
            self._write(envelope)
        return envelope
