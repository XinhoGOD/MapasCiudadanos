"""Build the app's Hidalgo-only catalog from the official INEGI AGEEML CSV.

The source CSV is downloaded separately because it is an external data artifact;
the generated GeoJSON contains only Hidalgo records and no survey data.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def build(source: Path, destination: Path) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    features: list[dict] = []
    municipalities: set[str] = set()
    try:
        handle = source.open("r", encoding="utf-8-sig", newline="")
        handle.read(4096)
        handle.seek(0)
    except UnicodeDecodeError:
        handle = source.open("r", encoding="cp1252", newline="")
    with handle:
        reader = csv.DictReader(handle)
        required = {"CVEGEO", "CVE_ENT", "NOM_ENT", "CVE_MUN", "NOM_MUN", "CVE_LOC", "NOM_LOC", "LAT_DECIMAL", "LON_DECIMAL"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"El CSV de INEGI no contiene las columnas requeridas: {sorted(missing)}")
        for row in reader:
            if row["CVE_ENT"].zfill(2) != "13":
                continue
            locality = (row.get("NOM_LOC") or "").strip()
            municipality = (row.get("NOM_MUN") or "").strip()
            try:
                latitude = float(row["LAT_DECIMAL"])
                longitude = float(row["LON_DECIMAL"])
            except (TypeError, ValueError):
                continue
            if not locality or not municipality:
                continue
            municipalities.add(municipality)
            properties = {
                "locality": locality,
                "municipality": municipality,
                "state": (row.get("NOM_ENT") or "Hidalgo").strip(),
                "official_key": row["CVEGEO"].strip(),
                "CVE_ENT": row["CVE_ENT"].zfill(2),
                "CVE_MUN": row["CVE_MUN"].zfill(3),
                "CVE_LOC": row["CVE_LOC"].zfill(4),
                "ambito": (row.get("AMBITO") or "").strip(),
            }
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [longitude, latitude]}, "properties": properties})

    date_match = re.search(r"AGEEML_(\d{4})(\d{2})\d{2}", source.name, re.IGNORECASE)
    source_cutoff = f"{date_match.group(1)}-{date_match.group(2)}" if date_match else "unknown"
    payload = {
        "type": "FeatureCollection",
        "name": f"inegi-ageeml-hidalgo-{source_cutoff}",
        "metadata": {
            "source": "INEGI — Catálogo Único de Claves de Áreas Geoestadísticas Estatales, Municipales y Localidades",
            "source_url": "https://www.inegi.org.mx/contenidos/app/ageeml/catun_localidad.zip",
            "source_cutoff": source_cutoff,
            "state": "Hidalgo",
            "CVE_ENT": "13",
            "coordinate_fields": "LAT_DECIMAL/LON_DECIMAL",
            "record_count": len(features),
            "municipality_count": len(municipalities),
        },
        "features": features,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(features), len(municipalities)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    records, municipalities = build(args.source, args.destination)
    print(f"Hidalgo catalog built: {records} localities in {municipalities} municipalities")


if __name__ == "__main__":
    main()
