"""Small, cached OpenStreetMap road-line adapter for map context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


OVERPASS_URLS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
USER_AGENT = "MapaParticipacionCiudadana/0.1 (+contexto-vial)"
ALLOWED_HIGHWAYS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "track",
    "path",
    "footway",
}
HIGHWAY_QUERY = "|".join(sorted(ALLOWED_HIGHWAYS))


def _coordinates(value: Any, output: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    if output is None:
        output = []
    if isinstance(value, list) and len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        output.append((float(value[0]), float(value[1])))
    elif isinstance(value, list):
        for item in value:
            _coordinates(item, output)
    return output


def _bbox(features: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    points = [point for feature in features for point in _coordinates(feature.get("geometry", {}).get("coordinates"))]
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return min(latitudes), min(longitudes), max(latitudes), max(longitudes)


def _cache_key(bbox: tuple[float, float, float, float]) -> str:
    value = ",".join(f"{part:.5f}" for part in bbox)
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:24]


def _geojson(elements: list[dict[str, Any]]) -> dict[str, Any]:
    features = []
    for element in elements:
        tags = element.get("tags") or {}
        highway = str(tags.get("highway") or "").strip()
        geometry = element.get("geometry") or []
        if highway not in ALLOWED_HIGHWAYS or len(geometry) < 2:
            continue
        coordinates = [[float(point["lon"]), float(point["lat"])] for point in geometry if "lon" in point and "lat" in point]
        if len(coordinates) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": {
                    "highway": highway,
                    "name": str(tags.get("name") or "").strip() or None,
                    "ref": str(tags.get("ref") or "").strip() or None,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def get_road_lines(
    features: list[dict[str, Any]],
    cache_dir: str | Path,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Return cached OSM highway lines for the municipality bounding box.

    The client clips these lines to the official INEGI municipality geometry.
    OSM is used only as visual context; no OSM names or geometry are used to
    join survey records.
    """
    bbox = bbox or _bbox(features)
    if not bbox:
        return {"type": "FeatureCollection", "features": [], "source": "OpenStreetMap", "features_count": 0}
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{_cache_key(bbox)}.json"
    if destination.exists() and destination.stat().st_size:
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                return payload
        except (OSError, json.JSONDecodeError):
            destination.unlink(missing_ok=True)

    south, west, north, east = bbox
    query = f'[out:json][timeout:20];way[highway~"^({HIGHWAY_QUERY})$"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});out geom;'
    last_error: Exception | None = None
    for overpass_url in OVERPASS_URLS:
        request = Request(
            overpass_url,
            data=query.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:  # nosec B310 - fixed public Overpass endpoints
                raw = response.read(12 * 1024 * 1024 + 1)
            if len(raw) > 12 * 1024 * 1024:
                raise ValueError("La respuesta de OpenStreetMap superó el límite de contexto permitido.")
            parsed = json.loads(raw.decode("utf-8"))
            result = _geojson(parsed.get("elements", []))
            result.update({"source": "OpenStreetMap", "features_count": len(result["features"])})
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(destination)
            return result
        except Exception as error:
            last_error = error

    return {
        "type": "FeatureCollection",
        "features": [],
        "source": "OpenStreetMap",
        "features_count": 0,
        "error": f"No fue posible cargar las líneas viales: {last_error}",
    }
