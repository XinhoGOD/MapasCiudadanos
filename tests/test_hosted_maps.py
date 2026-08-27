from pathlib import Path

import pytest
from starlette.requests import Request

from app.hosted_maps import HostedMapStore, PublicSheetError, _sheet_export_url
from scripts.hosted_map_server import _public_page


def test_sheet_export_url_is_restricted_to_public_google_sheets():
    url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=987"
    assert _sheet_export_url(url) == "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=987"

    with pytest.raises(PublicSheetError):
        _sheet_export_url("https://example.com/spreadsheets/d/abc123")


def test_store_persists_only_the_aggregate_snapshot(tmp_path: Path):
    result = {"map_type": "composition", "municipality": "Pacula", "features": []}
    store = HostedMapStore(tmp_path)
    envelope = store.create(result, {"type": "file", "label": "encuesta.xlsx"})

    loaded = store.get(envelope["map_id"])
    assert loaded["result"] == result
    assert loaded["source"]["label"] == "encuesta.xlsx"


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
