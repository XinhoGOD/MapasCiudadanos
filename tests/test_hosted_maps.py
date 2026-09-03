import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request

from app.hosted_maps import HostedMapStore, PublicSheetError, _sheet_export_url
from scripts.hosted_map_server import _map_reference, _municipality_slug, _osm_key, _preview_svg, _public_page, _resolve_map_id, osm_roads


def test_sheet_export_url_is_restricted_to_public_google_sheets():
    url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=987"
    assert _sheet_export_url(url) == "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=987"

    with pytest.raises(PublicSheetError):
        _sheet_export_url("https://example.com/spreadsheets/d/abc123")


def test_public_map_reference_includes_municipality_and_keeps_legacy_ids_working():
    map_id = "abcdefghijklmnop"
    reference = _map_reference(map_id, "San Agustín Tlaxiaca")

    assert _municipality_slug("San Agustín Tlaxiaca") == "san-agustin-tlaxiaca"
    assert reference == "abcdefghijklmnop-san-agustin-tlaxiaca"
    assert _resolve_map_id(reference) == map_id
    assert _resolve_map_id(map_id) == map_id


def test_store_persists_only_the_aggregate_snapshot(tmp_path: Path):
    result = {"map_type": "composition", "municipality": "Pacula", "features": []}
    store = HostedMapStore(tmp_path)
    envelope = store.create(result, {"type": "file", "label": "encuesta.xlsx"})

    loaded = store.get(envelope["map_id"])
    assert loaded["result"] == result
    assert loaded["source"]["label"] == "encuesta.xlsx"


def test_osm_endpoint_uses_persisted_artifact_without_requesting_overpass(tmp_path: Path, monkeypatch):
    store = HostedMapStore(tmp_path)
    envelope = store.create({"map_type": "dominant"}, {"type": "file"})
    stored = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-99, 21], [-98.9, 21.1]]}, "properties": {"highway": "primary"}}],
        "source": "OpenStreetMap",
        "features_count": 1,
    }
    store.write_json(_osm_key(envelope["map_id"]), stored)
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/maps/{envelope['map_id']}/osm-roads",
        "raw_path": f"/api/maps/{envelope['map_id']}/osm-roads".encode(),
        "query_string": b"bbox=-99,20,-98,22",
        "headers": [],
        "path_params": {"map_id": envelope["map_id"]},
        "server": ("127.0.0.1", 8770),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "http_version": "1.1",
    }
    monkeypatch.setattr("scripts.hosted_map_server.store", store)
    monkeypatch.setattr("scripts.hosted_map_server.get_road_lines", lambda *_args, **_kwargs: pytest.fail("No debe consultar OSM"))

    response = asyncio.run(osm_roads(Request(scope)))

    assert response.status_code == 200
    assert json.loads(response.body) == stored


def test_store_uses_blob_when_a_vercel_token_is_configured(tmp_path: Path, monkeypatch):
    class FakeBlob:
        objects = {}

        def __init__(self, token=None):
            assert token == "test-token"

        def put(self, path, body, **_):
            self.objects[path] = bytes(body)

        def get(self, path, **_):
            content = self.objects.get(path)
            return type("BlobResult", (), {"__bytes__": lambda self: content})() if content is not None else None

    import vercel.blob

    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "test-token")
    monkeypatch.setattr(vercel.blob, "BlobClient", FakeBlob)
    store = HostedMapStore(tmp_path)
    envelope = store.create({"map_type": "dominant"}, {"type": "file"})

    assert store.uses_persistent_storage is True
    assert store.get(envelope["map_id"])["result"]["map_type"] == "dominant"


def test_preview_svg_contains_map_and_legend():
    envelope = {
        "map_id": "abcdefghijklmnop",
        "result": {
            "municipality": "Pacula",
            "question": "Pregunta",
            "response_categories": ["A"],
            "background": {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-99, 21], [-98, 21], [-98, 20], [-99, 20], [-99, 21]]]}}]},
            "territory_background": {"features": []},
            "feature_collection": {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-99, 21], [-98.5, 21], [-98.5, 20.5], [-99, 21]]]}, "properties": {"locality": "Centro", "dominant_answer": "A"}}]},
            "influence_sites": [],
        },
    }

    svg = _preview_svg(envelope)

    assert svg.startswith("<svg")
    assert "Pacula" in svg
    assert "Respuesta predominante" in svg
    assert "Centro" in svg


def test_public_page_bootstraps_map_without_public_upload_controls(tmp_path: Path, monkeypatch):
    store = HostedMapStore(tmp_path)
    envelope = store.create(
        {"map_type": "composition", "municipality": "Pacula", "features": []},
        {"type": "file", "label": "encuesta.xlsx"},
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/maps/" + envelope["map_id"],
        "raw_path": b"/maps/" + envelope["map_id"].encode(),
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8770")],
        "server": ("127.0.0.1", 8770),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
    }
    request = Request(scope)
    monkeypatch.setattr("scripts.hosted_map_server.store", store)
    page = _public_page(envelope, request)

    assert '"publicMap":true' in page
    assert '"map_type":"composition"' in page
    assert 'publicMapVersionUrl' in page
    assert 'publicInformationUrl' in page
    assert 'Información' in page
    assert 'Carga de datos' not in page
    assert 'href="#alcance"' not in page


def test_home_navigation_is_minimal_and_accesses_planeacion_portal():
    home = Path("ui/hosted.html").read_text(encoding="utf-8")
    information = Path("ui/informacion.html").read_text(encoding="utf-8")

    assert "Transparencia" not in home
    assert "Servicios" not in home
    assert "Contacto" not in home
    assert 'href="https://u-planeacion.hidalgo.gob.mx/"' in home
    assert "Cómo interpretar el mapa territorial" in information
    assert "No debe concluirse" in information


def test_refresh_does_not_rewrite_unchanged_google_sheet(tmp_path: Path, monkeypatch):
    source_file = tmp_path / "source.xlsx"
    source_file.write_bytes(b"cached workbook")
    store = HostedMapStore(tmp_path / "store", refresh_seconds=15)
    envelope = store.create(
        {"map_type": "dominant", "version_marker": "original"},
        {
            "type": "google_sheets",
            "url": "https://docs.google.com/spreadsheets/d/test/edit",
            "sheet_name": "Respuestas",
            "municipality": "Pacula",
            "question": "Pregunta",
            "content_hash": "same-hash",
            "checked_at": "2020-01-01T00:00:00+00:00",
            "last_error": None,
        },
    )
    writes: list[dict] = []
    monkeypatch.setattr(store, "_write", lambda value: writes.append(value))
    monkeypatch.setattr(
        "app.hosted_maps.download_public_workbook",
        lambda _url, _cache: (source_file, "same-hash"),
    )

    refreshed = store.refresh_if_due(envelope["map_id"])

    assert refreshed["version"] == envelope["version"]
    assert writes == []
