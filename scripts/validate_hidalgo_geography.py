"""Validate the offline Hidalgo geography cache and write a provenance manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CATALOG_URL = "https://www.inegi.org.mx/contenidos/app/ageeml/catun_localidad.zip"
DCAH_URL = "https://www.inegi.org.mx/contenidos/productos/prod_serv/contenidos/espanol/bvinegi/productos/geografia/delimitaciones/794551163078/13_hidalgo.zip"
GAIA_URL = "https://gaia.inegi.org.mx/wscatgeo/v2"
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
FORBIDDEN_KINDS = {"estimated", "estimated_influence_zone", "idw_interpolation", "voronoi", "surface"}


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def coordinate_pairs(value: Any) -> Iterable[tuple[float, float]]:
    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
    elif isinstance(value, list):
        for child in value:
            yield from coordinate_pairs(child)


def validate_geometry(geometry: dict[str, Any] | None, label: str, polygon_only: bool = True) -> None:
    if not geometry:
        raise ValueError(f"{label}: falta geometría.")
    geometry_type = geometry.get("type")
    if polygon_only and geometry_type not in POLYGON_TYPES:
        raise ValueError(f"{label}: se esperaba Polygon/MultiPolygon y llegó {geometry_type!r}.")
    pairs = list(coordinate_pairs(geometry.get("coordinates")))
    if not pairs:
        raise ValueError(f"{label}: no contiene coordenadas.")
    if any(abs(longitude) > 180 or abs(latitude) > 90 for longitude, latitude in pairs):
        raise ValueError(f"{label}: contiene coordenadas fuera de WGS84 lon/lat.")


def validate(
    catalog_path: Path,
    geometry_root: Path,
    catalog_archive: Path,
    dcah_archive: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_features = catalog.get("features", [])
    municipality_codes: set[str] = set()
    for index, feature in enumerate(catalog_features):
        properties = feature.get("properties") or {}
        if str(properties.get("CVE_ENT") or "").zfill(2) != "13":
            raise ValueError(f"Catálogo, registro {index}: contiene una entidad distinta de Hidalgo.")
        if (feature.get("geometry") or {}).get("type") != "Point":
            raise ValueError(f"Catálogo, registro {index}: la referencia de localidad debe ser Point.")
        municipality_codes.add(str(properties.get("CVE_MUN") or "").zfill(3))

    files = sorted(geometry_root.glob("[0-9][0-9][0-9].geojson"))
    file_codes = {path.stem for path in files}
    if file_codes != municipality_codes:
        missing = sorted(municipality_codes - file_codes)
        extra = sorted(file_codes - municipality_codes)
        raise ValueError(f"La caché municipal no coincide con el catálogo. Faltan={missing}; sobran={extra}.")
    if len(files) != 84:
        raise ValueError(f"Se esperaban los 84 municipios de Hidalgo y se encontraron {len(files)}.")

    locality_polygons = 0
    settlement_polygons = 0
    combined_digest = hashlib.sha256()
    combined_digest.update(catalog_path.name.encode("utf-8"))
    combined_digest.update(catalog_path.read_bytes())
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = payload.get("metadata") or {}
        if str(metadata.get("CVE_ENT") or "").zfill(2) != "13" or str(metadata.get("CVE_MUN") or "").zfill(3) != path.stem:
            raise ValueError(f"{path.name}: metadatos de entidad/municipio inconsistentes.")
        validate_geometry((payload.get("municipality") or {}).get("geometry"), f"{path.name}: municipio")
        for index, feature in enumerate(payload.get("features", [])):
            properties = feature.get("properties") or {}
            geometry_kind = str(properties.get("geometry_kind") or ("settlement" if properties.get("settlement_type") else "locality")).casefold()
            source = str(properties.get("source") or "")
            if geometry_kind in FORBIDDEN_KINDS or any(token in geometry_kind for token in FORBIDDEN_KINDS):
                raise ValueError(f"{path.name}, unidad {index}: geometría sintética prohibida ({geometry_kind}).")
            if "INEGI" not in source:
                raise ValueError(f"{path.name}, unidad {index}: no declara una fuente INEGI.")
            validate_geometry(feature.get("geometry"), f"{path.name}, unidad {index}")
            if geometry_kind == "settlement" or properties.get("settlement_type"):
                settlement_polygons += 1
            else:
                locality_polygons += 1
        combined_digest.update(path.name.encode("utf-8"))
        combined_digest.update(path.read_bytes())

    counts = {
        "municipalities": len(files),
        "catalog_locality_points": len(catalog_features),
        "locality_polygons": locality_polygons,
        "settlement_polygons": settlement_polygons,
        "total_official_polygons": locality_polygons + settlement_polygons,
    }
    if not all(counts[key] > 0 for key in ("catalog_locality_points", "locality_polygons", "settlement_polygons")):
        raise ValueError(f"La caché oficial está incompleta: {counts}.")

    manifest = {
        "schema_version": 1,
        "state": "Hidalgo",
        "CVE_ENT": "13",
        "validated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "policy": "Sólo polígonos oficiales de INEGI; sin Voronoi, IDW, interpolación ni áreas inferidas.",
        "runtime_paths": {
            "catalog": str(catalog_path.relative_to(ROOT)).replace("\\", "/"),
            "municipal_geography": str(geometry_root.relative_to(ROOT)).replace("\\", "/") + "/<CVE_MUN>.geojson",
        },
        "sources": [
            {"name": "INEGI Catálogo Único AGEEML", "url": CATALOG_URL, "archive_sha256": sha256(catalog_archive)},
            {"name": "INEGI Marco Geoestadístico — Gaia", "url": GAIA_URL, "archive_sha256": None},
            {"name": "INEGI DCAH Hidalgo", "url": DCAH_URL, "archive_sha256": sha256(dcah_archive)},
        ],
        "counts": counts,
        "backend_dataset_sha256": combined_digest.hexdigest().upper(),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "geography.catalog.geojson")
    parser.add_argument("--geometry-root", type=Path, default=ROOT / "data" / "geography" / "hidalgo")
    parser.add_argument("--catalog-archive", type=Path, default=ROOT / "work" / "inegi-catun_localidad.zip")
    parser.add_argument("--dcah-archive", type=Path, default=ROOT / "work" / "inegi-dcah-hidalgo.zip")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data" / "inegi_hidalgo_manifest.json")
    args = parser.parse_args()
    manifest = validate(args.catalog, args.geometry_root, args.catalog_archive, args.dcah_archive, args.manifest)
    counts = manifest["counts"]
    print(
        "Caché de Hidalgo válida: "
        f"{counts['municipalities']} municipios, "
        f"{counts['locality_polygons']} polígonos de localidad y "
        f"{counts['settlement_polygons']} polígonos de asentamiento."
    )


if __name__ == "__main__":
    main()
