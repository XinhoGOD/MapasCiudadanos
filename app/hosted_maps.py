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


def _sheet_export_url(public_url: str, sheet_gid: str | None = None, file_format: str = "csv") -> str:
    parts = urlsplit(public_url.strip())
    if parts.scheme != "https" or parts.hostname != "docs.google.com" or parts.username or parts.password or parts.port:
        raise PublicSheetError("Usa un enlace HTTPS público de Google Sheets (docs.google.com).")
    match = SHEET_ID_PATTERN.search(parts.path)
    if not match:
        raise PublicSheetError("No pude identificar el ID de la hoja en el enlace proporcionado.")
    query = parse_qs(parts.query)
    gid = sheet_gid or query.get("gid", [None])[0]
    if not gid and parts.fragment:
        fragment_query = parse_qs(parts.fragment.lstrip("#?"))
        gid = fragment_query.get("gid", [None])[0]
    if file_format not in {"csv", "xlsx"}:
        raise PublicSheetError("El formato de exportación de Google Sheets no es válido.")
    parameters = {"format": file_format}
    if gid and re.fullmatch(r"\d+", gid):
        parameters["gid"] = gid
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?{urlencode(parameters)}"


def _download_public_file(
    public_url: str,
    cache_dir: str | Path,
    file_format: str,
    sheet_gid: str | None = None,
) -> tuple[Path, str]:
    export_url = _sheet_export_url(public_url, sheet_gid=sheet_gid, file_format=file_format)
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
    destination = cache / f"{url_key}.{file_format}"
    destination.write_bytes(content)
    return destination, hashlib.sha256(content).hexdigest()


def download_public_sheet(
    public_url: str,
    cache_dir: str | Path,
    sheet_gid: str | None = None,
) -> tuple[Path, str]:
    """Download one public sheet tab as CSV and return path plus content hash."""
    return _download_public_file(public_url, cache_dir, "csv", sheet_gid=sheet_gid)


def download_public_workbook(public_url: str, cache_dir: str | Path) -> tuple[Path, str]:
    """Download a public Google Sheets workbook so its tabs can be selected."""
    return _download_public_file(public_url, cache_dir, "xlsx")


class HostedMapStore:
    """Store aggregate snapshots locally or in Vercel Blob when configured."""

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
        self._blob_client = None
        blob_token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
        if blob_token:
            try:
                from vercel.blob import BlobClient
            except ImportError as error:  # pragma: no cover - exercised by deployment configuration
                raise RuntimeError(
                    "BLOB_READ_WRITE_TOKEN está configurado, pero falta la dependencia 'vercel'."
                ) from error
            self._blob_client = BlobClient(token=blob_token)
        configured_refresh = os.getenv("SHEET_REFRESH_SECONDS", "").strip()
        try:
            refresh_value = int(refresh_seconds) if refresh_seconds is not None else int(configured_refresh or "60")
        except (TypeError, ValueError):
            refresh_value = 60
        self.refresh_seconds = max(15, refresh_value)
        self._lock = threading.Lock()
        # Vercel instances can serve many version checks during the same warm
        # process.  Keep the probe timestamp in memory so an unchanged sheet
        # does not cause a Blob write every 30–60 seconds.  The persisted
        # timestamp still protects cold starts when it is available.
        self._last_refresh_probe: dict[str, datetime] = {}

    @property
    def uses_persistent_storage(self) -> bool:
        return self._blob_client is not None

    def _artifact_path(self, key: str) -> Path:
        return self.root / Path(key)

    def _write_artifact(self, key: str, content: bytes, content_type: str) -> None:
        if self._blob_client is not None:
            self._blob_client.put(
                f"mapas-ciudadanos/{key}",
                content,
                access="private",
                content_type=content_type,
                overwrite=True,
            )
            return
        target = self._artifact_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(target)

    def _read_artifact(self, key: str) -> bytes | None:
        if self._blob_client is not None:
            try:
                blob = self._blob_client.get(
                    f"mapas-ciudadanos/{key}",
                    access="private",
                    use_cache=False,
                )
            except Exception as error:
                if error.__class__.__name__ in {"BlobNotFoundError", "BlobPathnameMismatchError"}:
                    return None
                raise
            return bytes(blob) if blob is not None else None
        target = self._artifact_path(key)
        return target.read_bytes() if target.exists() else None

    def _has_artifact(self, key: str) -> bool:
        try:
            return self._read_artifact(key) is not None
        except Exception:
            return False

    def write_binary(self, key: str, content: bytes, content_type: str) -> None:
        """Persist a generated binary artifact such as a terrain or preview image."""
        self._write_artifact(key, content, content_type)

    def read_binary(self, key: str) -> bytes | None:
        """Read a persisted binary artifact, returning None when absent."""
        return self._read_artifact(key)

    def has_binary(self, key: str) -> bool:
        return self._has_artifact(key)

    def _path(self, map_id: str) -> Path:
        if not MAP_ID_PATTERN.fullmatch(map_id):
            raise ValueError("El identificador del mapa no es válido.")
        return self.root / f"{map_id}.json"

    def _write(self, envelope: dict[str, Any]) -> None:
        content = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self._write_artifact(f"{envelope['map_id']}.json", content, "application/json")

    def get(self, map_id: str) -> dict[str, Any]:
        self._path(map_id)
        content = self._read_artifact(f"{map_id}.json")
        if content is None:
            raise FileNotFoundError("No encontré el mapa solicitado.")
        return json.loads(content.decode("utf-8"))

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
        now = utc_now()
        checked_at = _parse_iso(source.get("checked_at"))
        last_probe = self._last_refresh_probe.get(map_id)
        latest_probe = max((value for value in (checked_at, last_probe) if value), default=None)
        if latest_probe and now - latest_probe < timedelta(seconds=self.refresh_seconds):
            return envelope

        with self._lock:
            envelope = self.get(map_id)
            source = envelope.get("source", {})
            now = utc_now()
            checked_at = _parse_iso(source.get("checked_at"))
            last_probe = self._last_refresh_probe.get(map_id)
            latest_probe = max((value for value in (checked_at, last_probe) if value), default=None)
            if latest_probe and now - latest_probe < timedelta(seconds=self.refresh_seconds):
                return envelope

            # Mark the probe before the network call.  If two requests arrive
            # together on one warm instance, only one of them downloads Sheets.
            self._last_refresh_probe[map_id] = now
            previous_hash = source.get("content_hash")
            previous_error = source.get("last_error")
            should_write = False
            try:
                sheet_path, content_hash = download_public_workbook(source["url"], self.source_cache)
                if content_hash != previous_hash:
                    result = generate_dominant_answer_map(
                        file_path=str(sheet_path),
                        municipality=source.get("municipality") or None,
                        question=source.get("question") or None,
                        sheet_name=source.get("sheet_name") or None,
                    )
                    envelope["result"] = result
                    envelope["version"] = _result_version(result)
                    envelope["updated_at"] = iso_now()
                    source["content_hash"] = content_hash
                    source["last_error"] = None
                    source["checked_at"] = iso_now()
                    should_write = True
                else:
                    source["last_error"] = None
            except Exception as error:  # keep the last known good map available
                source["last_error"] = str(error)
            # An unchanged sheet is the normal case.  Keep the last known map
            # in Blob and avoid rewriting its JSON just to update a heartbeat.
            # Errors are persisted only when their visible state changes.
            if source.get("last_error") != previous_error:
                source["checked_at"] = iso_now()
                should_write = True
            if should_write:
                envelope["source"] = source
                self._write(envelope)
        return envelope
