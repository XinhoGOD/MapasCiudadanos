from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import Transformer

from app.models.domain import GeographyMatch
from app.normalization import geography_name_variants, normalize_geography_name, normalize_text, similarity


@dataclass(frozen=True)
class GeographyRecord:
    locality: str
    municipality: str | None
    state: str | None
    official_key: str | None
    latitude: float | None
    longitude: float | None
    geometry: dict[str, Any] | None = None
    geometry_kind: str | None = None
    municipality_code: str | None = None


class GeographyRepository:
    """Resolve names against official Hidalgo polygons, loading one municipality at a time."""

    def __init__(self, catalog_path: str | None = None, geometry_root: str | None = None):
        configured_catalog = os.getenv("GEOGRAPHY_CATALOG_PATH", "").strip()
        self.catalog_path = catalog_path or configured_catalog or "data/geography.catalog.geojson"
        inferred_root = Path(self.catalog_path).parent / "geography" / "hidalgo" if catalog_path else Path("data/geography/hidalgo")
        configured_geometry_root = os.getenv("GEOMETRY_ROOT_PATH", "").strip()
        self.geometry_root = Path(geometry_root or configured_geometry_root or str(inferred_root))
        self.records = self._load_catalog(Path(self.catalog_path))
        # Runtime source of truth: one reviewed, offline GeoJSON per Hidalgo
        # municipality under data/geography/hidalgo. The two global files remain
        # optional compatibility fallbacks only when explicitly configured.
        localities_override = os.getenv("INEGI_LOCALITIES_PATH")
        municipalities_override = os.getenv("INEGI_MUNICIPALITIES_PATH")
        official_localities_path = Path(localities_override) if localities_override else Path("__no_official_localities__")
        official_municipalities_path = Path(municipalities_override) if municipalities_override else Path("__no_official_municipalities__")
        self.official_localities = self._load_official_localities(official_localities_path)
        self.official_municipalities = self._load_official_municipalities(official_municipalities_path)
        self._geometry_cache: dict[str, tuple[list[GeographyRecord], dict[str, Any] | None]] = {}

    def _load_official_localities(self, path: Path) -> dict[str, list[GeographyRecord]]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        grouped: dict[str, list[GeographyRecord]] = {}
        for feature in payload.get("features", []):
            properties = feature.get("properties") or {}
            raw_code = properties.get("cve_mun")
            if not raw_code:
                continue
            code = str(raw_code).zfill(3)
            row = {
                "locality": properties.get("nom_loc"),
                "official_key": properties.get("cvegeo"),
                "CVE_LOC": properties.get("cve_loc"),
                "CVE_MUN": code,
                "latitude": properties.get("latitud"),
                "longitude": properties.get("longitud"),
                "geometry_kind": "locality",
            }
            grouped.setdefault(code, []).append(self._record(row, self._normalize_geometry(feature.get("geometry"))))
        return grouped

    def _load_official_municipalities(self, path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for feature in payload.get("features", []):
            properties = feature.get("properties") or {}
            raw_code = properties.get("cve_mun")
            if raw_code:
                code = str(raw_code).zfill(3)
                normalized = dict(feature)
                normalized["geometry"] = self._normalize_geometry(feature.get("geometry"))
                result[code] = normalized
        return result

    @staticmethod
    def _normalize_geometry(geometry: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return GeoJSON coordinates in lon/lat WGS84 for the browser map.

        The INEGI DCAH settlement polygons in some Hidalgo files are stored in
        Mexico ITRF2008 Lambert Conformal Conic (EPSG:6372), while locality and
        municipality polygons are already lon/lat.  D3 cannot project the
        meter-based coordinates directly, so detect them by their magnitude and
        normalize only those geometries.
        """
        if not geometry or not geometry.get("coordinates"):
            return geometry

        def first_pair(value: Any) -> tuple[float, float] | None:
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                    return float(value[0]), float(value[1])
                for child in value:
                    pair = first_pair(child)
                    if pair:
                        return pair
            return None

        sample = first_pair(geometry["coordinates"])
        if not sample or (abs(sample[0]) <= 180 and abs(sample[1]) <= 90):
            return geometry

        transformer = Transformer.from_crs("EPSG:6372", "EPSG:4326", always_xy=True)

        def transform(value: Any) -> Any:
            if isinstance(value, list) and len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                longitude, latitude = transformer.transform(float(value[0]), float(value[1]))
                return [longitude, latitude, *value[2:]]
            if isinstance(value, list):
                return [transform(child) for child in value]
            return value

        normalized = dict(geometry)
        normalized["coordinates"] = transform(geometry["coordinates"])
        normalized.pop("crs", None)
        return normalized

    def _load_catalog(self, path: Path) -> list[GeographyRecord]:
        if not path.exists():
            return []
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return [self._record(row) for row in csv.DictReader(handle)]
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records: list[GeographyRecord] = []
        for feature in payload.get("features", []):
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            longitude, latitude = self._point_from_geometry(geometry.get("type"), geometry.get("coordinates"))
            records.append(self._record({**properties, "latitude": latitude, "longitude": longitude}))
        return records

    @staticmethod
    def _point_from_geometry(kind: str | None, coordinates: Any) -> tuple[float | None, float | None]:
        if kind == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
            return float(coordinates[0]), float(coordinates[1])
        return None, None

    @staticmethod
    def _record(row: dict[str, Any], geometry: dict[str, Any] | None = None) -> GeographyRecord:
        locality = row.get("locality") or row.get("NOMGEO") or row.get("NOM_LOC") or row.get("NOM_ASEN") or row.get("nombre") or ""
        municipality = row.get("municipality") or row.get("NOM_MUN") or row.get("municipio")
        state = row.get("state") or row.get("NOM_ENT") or row.get("estado")
        key = row.get("official_key") or row.get("CVE_LOC") or row.get("CVEGEO") or row.get("cvegeo")
        lat, lon = row.get("latitude") or row.get("latitud"), row.get("longitude") or row.get("longitud")
        return GeographyRecord(
            str(locality).strip(),
            str(municipality).strip() if municipality else None,
            str(state).strip() if state else None,
            str(key).strip() if key else None,
            float(lat) if lat not in (None, "") else None,
            float(lon) if lon not in (None, "") else None,
            geometry=geometry,
            geometry_kind=row.get("geometry_kind"),
            municipality_code=str(row.get("CVE_MUN") or "").zfill(3) if row.get("CVE_MUN") else None,
        )

    def _municipality_code(self, municipality: str | None) -> str | None:
        if not municipality:
            return None
        exact = [record.municipality_code for record in self.records if record.municipality_code and normalize_text(record.municipality) == normalize_text(municipality)]
        return exact[0] if exact else None

    def _load_geometry(self, municipality: str | None) -> tuple[list[GeographyRecord], dict[str, Any] | None]:
        code = self._municipality_code(municipality)
        if not code:
            return [], None
        if code in self._geometry_cache:
            return self._geometry_cache[code]
        path = self.geometry_root / f"{code}.geojson"
        if not path.exists():
            fallback = (list(self.official_localities.get(code, [])), self.official_municipalities.get(code))
            self._geometry_cache[code] = fallback
            return self._geometry_cache[code]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._geometry_cache[code] = ([], None)
            return self._geometry_cache[code]
        records: list[GeographyRecord] = []
        for feature in payload.get("features", []):
            properties = dict(feature.get("properties") or {})
            is_settlement = bool(properties.get("settlement_type") or properties.get("CVE_ASEN") or properties.get("cve_asen"))
            properties["geometry_kind"] = "settlement" if is_settlement else (properties.get("geometry_kind") or "locality")
            geometry = self._normalize_geometry(feature.get("geometry"))
            if geometry and geometry.get("type") in {"Polygon", "MultiPolygon"}:
                records.append(self._record(properties, geometry))
        municipality_feature = payload.get("municipality") or self.official_municipalities.get(code)
        if municipality_feature:
            municipality_feature = dict(municipality_feature)
            municipality_feature["geometry"] = self._normalize_geometry(municipality_feature.get("geometry"))
        self._geometry_cache[code] = (records, municipality_feature)
        return self._geometry_cache[code]

    def _geometry_records(self, municipality: str | None) -> list[GeographyRecord]:
        records, _ = self._load_geometry(municipality)
        return records

    def background(self, municipality: str | None) -> dict[str, Any] | None:
        code = self._municipality_code(municipality)
        if not code:
            return None
        path = self.geometry_root / f"{code}.geojson"
        if not path.exists():
            fallback = self.official_municipalities.get(code)
            return {"type": "FeatureCollection", "features": [fallback], "metadata": {"source": "INEGI — Marco Geoestadístico"}} if fallback else None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        feature = payload.get("municipality")
        if not feature:
            return None
        feature = dict(feature)
        feature["geometry"] = self._normalize_geometry(feature.get("geometry"))
        return {"type": "FeatureCollection", "features": [feature], "metadata": payload.get("metadata", {})}

    def territory_background(self, municipality: str | None) -> dict[str, Any]:
        """Return every official locality/settlement polygon for the municipality."""
        features = []
        for record in self._geometry_records(municipality):
            if not record.geometry or record.geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                continue
            features.append({
                "type": "Feature",
                "geometry": record.geometry,
                "properties": {
                    "locality": record.locality,
                    "official_key": record.official_key,
                    "geometry_kind": record.geometry_kind or "locality",
                    "source": "INEGI DCAH" if record.geometry_kind == "settlement" else "INEGI Marco Geoestadístico",
                },
            })
        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "state": "Hidalgo",
                "municipality": municipality,
                "official_only": True,
                "polygon_count": len(features),
                "policy": "No se generan Voronoi, interpolaciones ni límites sintéticos.",
            },
        }

    @staticmethod
    def _representative_point(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
        """Return a stable centroid-like site from an official polygon."""
        if not geometry:
            return None
        coordinates = geometry.get("coordinates") or []
        polygons = coordinates if geometry.get("type") == "MultiPolygon" else [coordinates] if geometry.get("type") == "Polygon" else []
        rings = [polygon[0] for polygon in polygons if polygon and polygon[0]]
        if not rings:
            return None

        def ring_area(ring: list[list[float]]) -> float:
            return sum(
                float(ring[index][0]) * float(ring[(index + 1) % len(ring)][1])
                - float(ring[(index + 1) % len(ring)][0]) * float(ring[index][1])
                for index in range(len(ring))
            )

        ring = max(rings, key=lambda item: abs(ring_area(item)))
        area_twice = ring_area(ring)
        if abs(area_twice) < 1e-12:
            return (
                sum(float(point[0]) for point in ring) / len(ring),
                sum(float(point[1]) for point in ring) / len(ring),
            )
        centroid_x = 0.0
        centroid_y = 0.0
        for index, point in enumerate(ring):
            next_point = ring[(index + 1) % len(ring)]
            cross = float(point[0]) * float(next_point[1]) - float(next_point[0]) * float(point[1])
            centroid_x += (float(point[0]) + float(next_point[0])) * cross
            centroid_y += (float(point[1]) + float(next_point[1])) * cross
        return centroid_x / (3 * area_twice), centroid_y / (3 * area_twice)

    def analysis_site(self, locality: str, municipality: str | None = None) -> dict[str, Any] | None:
        """Resolve a locality to a point suitable for an explicitly analytical influence view."""
        polygon_match = self.resolve(locality, municipality)
        if polygon_match.geometry is not None and polygon_match.level in {"EXACT", "NORMALIZED_EXACT", "ALIAS", "FUZZY_HIGH"}:
            # For locality polygons, prefer the matching official AGEEML point.
            if polygon_match.geometry_kind != "settlement":
                exact_catalog = [
                    record
                    for record in self.records
                    if record.longitude is not None
                    and record.latitude is not None
                    and normalize_text(record.municipality) == normalize_text(municipality)
                    and (
                        (polygon_match.official_key and record.official_key == polygon_match.official_key)
                        or normalize_text(record.locality) == normalize_text(polygon_match.matched_name)
                    )
                ]
                if exact_catalog:
                    record = exact_catalog[0]
                    return {
                        "locality": polygon_match.matched_name or locality,
                        "official_key": polygon_match.official_key or record.official_key,
                        "longitude": record.longitude,
                        "latitude": record.latitude,
                        "geometry_kind": polygon_match.geometry_kind or "locality",
                        "site_source": "INEGI AGEEML — coordenada oficial de localidad",
                        "match_level": polygon_match.level,
                    }
            representative = self._representative_point(polygon_match.geometry)
            if representative:
                return {
                    "locality": polygon_match.matched_name or locality,
                    "official_key": polygon_match.official_key,
                    "longitude": representative[0],
                    "latitude": representative[1],
                    "geometry_kind": polygon_match.geometry_kind or "locality",
                    "site_source": "Centroide analítico de un polígono oficial INEGI",
                    "match_level": polygon_match.level,
                }

        point_match = self.resolve_point(locality, municipality)
        if point_match.level in {"POINT_EXACT", "POINT_ALIAS"} and point_match.longitude is not None and point_match.latitude is not None:
            return {
                "locality": point_match.matched_name or locality,
                "official_key": point_match.official_key,
                "longitude": point_match.longitude,
                "latitude": point_match.latitude,
                "geometry_kind": "point_locality",
                "site_source": "INEGI AGEEML — coordenada oficial de localidad",
                "match_level": point_match.level,
            }
        return None

    def resolve_point(self, locality: str, municipality: str | None = None) -> GeographyMatch:
        """Classify names present only in INEGI's official point catalogue.

        Point matches are diagnostic only. They are never converted into an area
        because a point is not a valid choropleth boundary.
        """
        code = self._municipality_code(municipality)
        pool = [record for record in self.records if (not code or record.municipality_code == code or normalize_text(record.municipality) == normalize_text(municipality))]
        pool = [record for record in pool if record.longitude is not None and record.latitude is not None]
        if not pool:
            return GeographyMatch(locality, None, "UNMATCHED", 0.0, warning="No hay puntos oficiales cargados para el municipio solicitado.")
        normalized_input = normalize_geography_name(locality)
        exact = [record for record in pool if normalize_geography_name(record.locality) == normalized_input]
        if exact:
            return self._match(
                locality,
                exact[0],
                "POINT_EXACT",
                1.0,
                "INEGI registra esta localidad como punto, pero no publica un polígono oficial compatible; no se coloreó como área.",
            )
        for alias in self._alias_names(locality, municipality):
            alias_match = [record for record in pool if alias in geography_name_variants(record.locality)]
            if alias_match:
                return self._match(
                    locality,
                    alias_match[0],
                    "POINT_ALIAS",
                    0.97,
                    "El nombre se normalizó contra el catálogo puntual de INEGI, pero no existe un polígono oficial compatible.",
                )

        contextual = self._contextual_matches(locality, pool)
        if contextual:
            return self._match(
                locality,
                self._prefer_locality(contextual),
                "POINT_ALIAS",
                0.96,
                "El nombre se vinculó por componentes contra la localidad oficial del municipio, pero no existe un polígono compatible.",
            )
        scored = self._rank_candidates(locality, pool)
        score, record, margin = scored[0]
        if score >= 0.90 and margin >= 0.04:
            return self._match(
                locality,
                record,
                "POINT_FUZZY_HIGH",
                score,
                "Hay un punto oficial de nombre similar, pero requiere revisión y no se dibujó como área.",
            )
        return GeographyMatch(locality, None, "UNMATCHED", score, warning="No se encontró un punto oficial confiable para esta localidad.")

    @staticmethod
    def _alias_names(locality: str, municipality: str | None) -> list[str]:
        """Return conservative aliases that do not change territorial meaning."""
        normalized = normalize_geography_name(locality)
        municipality_name = normalize_text(municipality)
        aliases: list[str] = geography_name_variants(locality)[1:]
        for suffix in (" hidalgo", " hgo"):
            if normalized.endswith(suffix):
                aliases.append(normalized[: -len(suffix)].strip())
        if municipality_name and normalized in {f"{municipality_name} hidalgo", f"{municipality_name} hgo"}:
            aliases.append(municipality_name)
        return list(dict.fromkeys(alias for alias in aliases if alias and alias != normalized))

    @staticmethod
    def _rank_candidates(locality: str, pool: list[GeographyRecord]) -> list[tuple[float, GeographyRecord, float]]:
        """Rank typo-tolerant candidates and expose the confidence margin.

        Ratio and token-sort ratio catch spelling mistakes and word-order
        differences without the aggressive partial matching that can merge a
        short locality into a longer, unrelated name. A margin is required so
        two similarly named localities are never silently merged.
        """
        input_keys = geography_name_variants(locality) or [normalize_text(locality)]
        ranked: list[tuple[float, GeographyRecord]] = []
        for record in pool:
            record_keys = geography_name_variants(record.locality) or [normalize_text(record.locality)]
            score = max(
                similarity(input_key, record_key)
                for input_key in input_keys
                for record_key in record_keys
            )
            ranked.append((score, record))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return []
        best_score = ranked[0][0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - second_score
        return [(score, record, margin if index == 0 else score - best_score) for index, (score, record) in enumerate(ranked)]

    def resolve(self, locality: str, municipality: str | None = None) -> GeographyMatch:
        pool = self._geometry_records(municipality)
        if not pool:
            return GeographyMatch(locality, None, "UNMATCHED", 0.0, warning="No hay polígonos oficiales cargados para el municipio solicitado.")

        exact = [record for record in pool if record.locality == locality]
        if exact:
            return self._match(locality, self._prefer_locality(exact), "EXACT", 1.0)
        normalized_input = normalize_geography_name(locality)
        normalized = [record for record in pool if normalize_geography_name(record.locality) == normalized_input]
        if normalized:
            return self._match(locality, self._prefer_locality(normalized), "NORMALIZED_EXACT", 0.99)

        for alias in self._alias_names(locality, municipality):
            alias_matches = [record for record in pool if alias in geography_name_variants(record.locality)]
            if alias_matches:
                return self._match(
                    locality,
                    self._prefer_locality(alias_matches),
                    "ALIAS",
                    0.97,
                    "Se eliminó únicamente el sufijo estatal del nombre del Excel.",
                )

        contextual = self._contextual_matches(locality, pool)
        if contextual:
            return self._match(
                locality,
                self._prefer_locality(contextual),
                "ALIAS",
                0.96,
                "El nombre se vinculó automáticamente por sus componentes contra el catálogo oficial de INEGI.",
            )

        scored = self._rank_candidates(locality, pool)
        score, record, margin = scored[0]
        if score >= 0.90 and margin >= 0.04:
            return self._match(locality, record, "FUZZY_HIGH", score, "Coincidencia ortográfica automática contra el catálogo oficial de INEGI.")
        if score >= 0.78:
            return self._match(locality, record, "FUZZY_REVIEW", score, "Coincidencia de baja confianza; no se acepta automáticamente.")
        return GeographyMatch(locality, None, "UNMATCHED", score, warning="No se encontró un polígono oficial confiable.")

    @staticmethod
    def _contextual_matches(locality: str, pool: list[GeographyRecord]) -> list[GeographyRecord]:
        """Find unique official names contained in an abbreviated response.

        This handles forms such as ``San Nicolás`` → ``San Nicolás
        Coatzontla`` and ``Itztacoyotla`` → ``San Lorenzo Itztacoyotla``.
        It is deliberately used only after exact/alias matching and only when
        one official key wins inside the already municipality-filtered pool.
        """
        input_variants = geography_name_variants(locality)
        if not input_variants:
            return []
        candidates: list[tuple[int, int, GeographyRecord]] = []
        for record in pool:
            record_variants = geography_name_variants(record.locality)
            best: tuple[int, int] | None = None
            for input_key in input_variants:
                input_tokens = input_key.split()
                if not input_tokens:
                    continue
                for record_key in record_variants:
                    record_tokens = record_key.split()
                    if not record_tokens or input_key == record_key:
                        continue
                    # A phrase may be a shortened official name or may contain
                    # an extra neighborhood qualifier. Require at least one
                    # meaningful token and contiguous phrase containment.
                    if GeographyRepository._contains_token_phrase(input_tokens, record_tokens) or GeographyRepository._contains_token_phrase(record_tokens, input_tokens):
                        shared = min(len(input_tokens), len(record_tokens))
                        extra = abs(len(input_tokens) - len(record_tokens))
                        score = (shared, -extra)
                        if best is None or score > best:
                            best = score
            if best is not None:
                candidates.append((best[0], best[1], record))

        if not candidates:
            return []
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_shared, best_extra, _ = candidates[0]
        top = [item for item in candidates if item[0] == best_shared and item[1] == best_extra]
        official_keys = {item[2].official_key or normalize_geography_name(item[2].locality) for item in top}
        if len(official_keys) != 1:
            return []
        return [item[2] for item in top]

    @staticmethod
    def _contains_token_phrase(shorter: list[str], longer: list[str]) -> bool:
        """Return whether one complete token phrase occurs in the other."""
        if not shorter or len(shorter) > len(longer):
            return False
        width = len(shorter)
        return any(longer[index:index + width] == shorter for index in range(len(longer) - width + 1))

    @staticmethod
    def _prefer_locality(records: list[GeographyRecord]) -> GeographyRecord:
        return sorted(records, key=lambda record: 0 if record.geometry_kind == "locality" else 1)[0]

    @staticmethod
    def _match(input_name: str, record: GeographyRecord, level: str, confidence: float, warning: str | None = None) -> GeographyMatch:
        return GeographyMatch(
            input_name,
            record.locality,
            level,
            round(confidence, 3),
            record.official_key,
            record.latitude,
            record.longitude,
            warning,
            record.geometry,
            record.geometry_kind,
        )
