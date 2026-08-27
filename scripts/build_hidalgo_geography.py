"""Download and cache official INEGI polygons for Hidalgo by municipality."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import shapefile
from pyproj import Transformer


ROOT = Path(__file__).resolve().parents[1]
GAIA = "https://gaia.inegi.org.mx/wscatgeo/v2"
DCAH_URL = "https://www.inegi.org.mx/contenidos/productos/prod_serv/contenidos/espanol/bvinegi/productos/geografia/delimitaciones/794551163078/13_hidalgo.zip"


def get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "mapa-participacion-ciudadana/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def transform_coordinates(value: Any, transformer: Transformer) -> Any:
    if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        x, y = transformer.transform(value[0], value[1])
        return [round(x, 7), round(y, 7), *value[2:]]
    if isinstance(value, (list, tuple)):
        return [transform_coordinates(item, transformer) for item in value]
    return value


def read_settlement_features(shp_path: Path) -> dict[str, list[dict[str, Any]]]:
    reader = shapefile.Reader(str(shp_path), encoding="latin1")
    fields = [field[0] for field in reader.fields[1:]]
    source_crs = shp_path.with_suffix(".prj").read_text(encoding="utf-8")
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for shape_record in reader.iterShapeRecords():
        properties = dict(zip(fields, list(shape_record.record)))
        cve_mun = str(properties.get("cve_mun") or "").zfill(3)
        geometry = shape_record.shape.__geo_interface__
        geometry = {**geometry, "coordinates": transform_coordinates(geometry["coordinates"], transformer)}
        clean_properties = {
            "locality": str(properties.get("nom_asen") or "").strip(),
            "official_key": str(properties.get("cvegeo") or "").strip(),
            "CVE_ENT": str(properties.get("cve_ent") or "13").zfill(2),
            "CVE_MUN": cve_mun,
            "CVE_LOC": str(properties.get("cve_loc") or "").zfill(4),
            "CVE_ASEN": str(properties.get("cve_asen") or "").zfill(4),
            "settlement_type": str(properties.get("tipo") or "").strip(),
            "source": "INEGI DCAH 2025",
        }
        grouped.setdefault(cve_mun, []).append({"type": "Feature", "geometry": geometry, "properties": clean_properties})
    return grouped


def annotate_feature(feature: dict[str, Any], kind: str) -> dict[str, Any]:
    properties = dict(feature.get("properties") or {})
    if kind == "municipality":
        properties = {
            "municipality": properties.get("nomgeo"),
            "official_key": properties.get("cvegeo"),
            "CVE_ENT": properties.get("cve_ent"),
            "CVE_MUN": properties.get("cve_mun"),
            "geometry_kind": "municipality",
            "source": "INEGI Gaia — Servicio vectorial del Marco Geoestadístico",
        }
    else:
        properties = {
            "locality": properties.get("nom_loc"),
            "municipality": properties.get("nom_mun"),
            "official_key": properties.get("cvegeo"),
            "CVE_ENT": properties.get("cve_ent"),
            "CVE_MUN": properties.get("cve_mun"),
            "CVE_LOC": properties.get("cve_loc"),
            "ambito": properties.get("ambito"),
            "geometry_kind": "locality",
            "source": "INEGI Gaia — Servicio vectorial del Marco Geoestadístico",
        }
    return {"type": "Feature", "geometry": feature.get("geometry"), "properties": properties}


def build(destination: Path, settlement_shp: Path) -> tuple[int, int, int]:
    municipalities_payload = get_json(f"{GAIA}/geo/mgem/13")
    settlements = read_settlement_features(settlement_shp)
    destination.mkdir(parents=True, exist_ok=True)
    locality_count = 0
    settlement_count = 0
    for municipality_feature in municipalities_payload.get("features", []):
        properties = municipality_feature.get("properties") or {}
        cve_mun = str(properties.get("cve_mun") or "").zfill(3)
        if not cve_mun:
            continue
        localities_payload = get_json(f"{GAIA}/geo/localidades/pol/13/{cve_mun}")
        municipality = annotate_feature(municipality_feature, "municipality")
        locality_features = [annotate_feature(feature, "locality") for feature in localities_payload.get("features", [])]
        settlement_features = settlements.get(cve_mun, [])
        locality_count += len(locality_features)
        settlement_count += len(settlement_features)
        output = {
            "type": "FeatureCollection",
            "name": f"inegi-hidalgo-{cve_mun}",
            "metadata": {
                "state": "Hidalgo",
                "CVE_ENT": "13",
                "CVE_MUN": cve_mun,
                "municipality": properties.get("nomgeo"),
                "sources": [
                    "INEGI Gaia — Servicio vectorial del Marco Geoestadístico",
                    "INEGI DCAH 2025",
                ],
                "source_urls": [
                    f"{GAIA}/geo/mgem/13",
                    f"{GAIA}/geo/localidades/pol/13/{cve_mun}",
                    DCAH_URL,
                ],
                "geometry_policy": "Solo se publican polígonos de INEGI; no se generan puntos ni geometrías sintéticas.",
                "locality_polygon_count": len(locality_features),
                "settlement_polygon_count": len(settlement_features),
            },
            "municipality": municipality,
            "features": locality_features + settlement_features,
        }
        (destination / f"{cve_mun}.geojson").write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(municipalities_payload.get("features", [])), locality_count, settlement_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=ROOT / "data" / "geography" / "hidalgo")
    parser.add_argument("--settlement-zip", type=Path, default=ROOT / "work" / "inegi-dcah-hidalgo.zip")
    args = parser.parse_args()
    extract_dir = args.settlement_zip.parent / "inegi-dcah-hidalgo"
    with zipfile.ZipFile(args.settlement_zip) as archive:
        archive.extractall(extract_dir)
    result = build(args.destination, extract_dir / "conjunto_de_datos" / "13as.shp")
    print(f"Municipios: {result[0]}; polígonos de localidad: {result[1]}; asentamientos/colonias: {result[2]}")


if __name__ == "__main__":
    sys.exit(main())
