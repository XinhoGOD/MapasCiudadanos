from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Protocol


class MapExporter(Protocol):
    def export(self, map_result: dict, destination: Path) -> Path: ...


def export_geojson(map_result: dict, destination: str | Path) -> Path:
    """Write only the aggregated feature collection; no source rows are exported."""
    path = Path(destination)
    path.write_text(json.dumps(map_result.get("feature_collection", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_frequency_csv(map_result: dict, destination: str | Path) -> Path:
    path = Path(destination)
    rows = map_result.get("ranking", [])
    fieldnames = ["rank", "locality", "frequency", "total_participations", "percentage_locality"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
