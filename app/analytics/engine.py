from __future__ import annotations

from collections import Counter
from functools import lru_cache
from typing import Any

import pandas as pd

from app.geography import GeographyRepository
from app.models.domain import MapResult
from app.normalization import normalize_text, similarity, split_answers
from app.survey import choose_question, detect_schema, load_dataset
from app.survey.semantics import choose_column, question_options


ACCEPTED_POLYGON_LEVELS = {"EXACT", "NORMALIZED_EXACT", "ALIAS", "FUZZY_HIGH"}
EXACT_POINT_LEVELS = {"POINT_EXACT", "POINT_ALIAS"}
MISSING_LOCALITY = "(Sin localidad declarada)"


@lru_cache(maxsize=8)
def _geography_repository(catalog_path: str | None) -> GeographyRepository:
    """Reuse the parsed Hidalgo catalogue on warm server instances."""
    return GeographyRepository(catalog_path)


def _single_frame(dataset):
    return dataset.frame.copy()


def inspect_dataset(file_path: str | None = None, file: Any = None, sheet_name: str | None = None) -> dict[str, Any]:
    dataset = load_dataset(file_path, file, sheet_name=sheet_name)
    sheet_summaries = [
        {"name": name, "records": int(len(frame)), "columns": [str(column) for column in frame.columns]}
        for name, frame in dataset.frames.items()
    ]
    schema = detect_schema(_single_frame(dataset))
    warnings = list(dataset.warnings)
    if not schema["municipality_candidates"]:
        warnings.append("No pude identificar una columna de municipio en este archivo.")
    if not schema["locality_candidates"]:
        warnings.append("No pude identificar una columna de localidad en este archivo.")
    if not schema["questions"]:
        warnings.append("No pude identificar preguntas categóricas para analizar.")
    municipality_column = choose_column(schema, "municipality")
    municipalities = []
    if municipality_column:
        municipalities = list(
            dict.fromkeys(
                str(value).strip()
                for value in dataset.frame[municipality_column].dropna().tolist()
                if str(value).strip()
            )
        )
    return {
        "source_name": dataset.source_name,
        "sheets": sheet_summaries,
        "records": dataset.record_count,
        "schema": schema,
        "municipalities": municipalities[:200],
        "warnings": warnings,
    }


def _context(
    dataset,
    municipality: str | None,
    question: str | None,
    municipality_column: str | None = None,
):
    frame = _single_frame(dataset)
    schema = detect_schema(frame)
    municipality_column = choose_column(schema, "municipality", municipality_column)
    locality_column = choose_column(schema, "locality")
    question = choose_question(schema, question)
    if not locality_column:
        raise ValueError("No pude identificar una columna de localidad en este archivo.")

    resolved_municipality = municipality
    if municipality_column:
        display_values = [
            str(value).strip()
            for value in frame[municipality_column].dropna().tolist()
            if str(value).strip()
        ]
        unique_municipalities = list(dict.fromkeys(display_values))
        if municipality:
            exact = [
                value
                for value in unique_municipalities
                if normalize_text(value) == normalize_text(municipality)
            ]
            if exact:
                resolved_municipality = exact[0]
            else:
                fuzzy = sorted(
                    ((similarity(municipality, value), value) for value in unique_municipalities),
                    reverse=True,
                )
                if fuzzy and fuzzy[0][0] >= 0.86:
                    resolved_municipality = fuzzy[0][1]
                else:
                    raise ValueError("No encontré el municipio solicitado en el archivo.")
            frame = frame[
                frame[municipality_column].map(normalize_text)
                == normalize_text(resolved_municipality)
            ].copy()
        elif len(unique_municipalities) == 1:
            resolved_municipality = unique_municipalities[0]
        elif len(unique_municipalities) > 1:
            options = ", ".join(unique_municipalities[:8])
            raise ValueError(
                f"El archivo contiene varios municipios ({options}). Indica cuál quieres representar."
            )
    elif not municipality:
        raise ValueError(
            "No pude identificar una columna de municipio. Indica el municipio para evitar coincidencias geográficas ambiguas."
        )
    if frame.empty:
        raise ValueError("No encontré registros para el municipio solicitado.")
    return frame, schema, municipality_column, locality_column, question, resolved_municipality


def resolve_geography(
    file_path: str | None = None,
    file: Any = None,
    municipality: str | None = None,
    question: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    """Validate locality names before rendering a map.

    Only rows with a valid response to the selected question are considered.
    Polygon matches and exact official point matches are safe to represent;
    fuzzy suggestions remain explicitly separated for review.
    """
    dataset = load_dataset(file_path, file)
    frame, _, _, locality_column, chosen_question, resolved_municipality = _context(
        dataset, municipality, question
    )
    if question and not chosen_question:
        raise ValueError("No pude identificar la pregunta seleccionada en el archivo.")
    if chosen_question:
        frame = frame[frame[chosen_question].map(lambda value: bool(_response_items(value)))].copy()

    repo = _geography_repository(catalog_path)
    matches: list[dict[str, Any]] = []
    for locality, group in frame.groupby(locality_column, dropna=False):
        records = int(len(group))
        if pd.isna(locality) or not str(locality).strip():
            matches.append(
                {
                    "input_name": MISSING_LOCALITY,
                    "matched_name": None,
                    "level": "MISSING_LOCALITY",
                    "confidence": 0.0,
                    "official_key": None,
                    "geometry": None,
                    "geometry_kind": None,
                    "classification": "missing_locality",
                    "records": records,
                    "warning": "El registro no contiene localidad o colonia.",
                }
            )
            continue

        display = str(locality).strip()
        polygon_match = repo.resolve(display, resolved_municipality)
        if polygon_match.geometry is not None and polygon_match.level in ACCEPTED_POLYGON_LEVELS:
            match = polygon_match
            classification = "official_polygon"
        else:
            point_match = repo.resolve_point(display, resolved_municipality)
            if point_match.level in EXACT_POINT_LEVELS:
                match = point_match
                classification = "analytical_site"
            elif polygon_match.level == "FUZZY_REVIEW" or point_match.level == "POINT_FUZZY_HIGH":
                match = point_match if point_match.level == "POINT_FUZZY_HIGH" else polygon_match
                classification = "review_required"
            else:
                match = polygon_match
                classification = "unmatched"

        matches.append(
            {
                "input_name": display,
                "matched_name": match.matched_name,
                "level": match.level,
                "confidence": match.confidence,
                "official_key": match.official_key,
                "geometry": match.geometry,
                "geometry_kind": match.geometry_kind,
                "classification": classification,
                "records": records,
                "warning": match.warning,
            }
        )

    official = [item for item in matches if item["classification"] == "official_polygon"]
    point_sites = [item for item in matches if item["classification"] == "analytical_site"]
    review = [item for item in matches if item["classification"] == "review_required"]
    unmatched = [item for item in matches if item["classification"] == "unmatched"]
    missing = [item for item in matches if item["classification"] == "missing_locality"]
    representable = [*official, *point_sites]
    unit_identity = lambda item: (
        item.get("official_key")
        or normalize_text(item.get("matched_name"))
        or normalize_text(item.get("input_name"))
    )
    official_units = {unit_identity(item) for item in official}
    point_units = {unit_identity(item) for item in point_sites}
    representable_units = official_units | point_units
    total_records = int(sum(item["records"] for item in matches))
    representable_records = int(sum(item["records"] for item in representable))
    levels = (
        "EXACT",
        "NORMALIZED_EXACT",
        "ALIAS",
        "FUZZY_HIGH",
        "FUZZY_REVIEW",
        "POINT_EXACT",
        "POINT_ALIAS",
        "POINT_FUZZY_HIGH",
        "UNMATCHED",
        "MISSING_LOCALITY",
    )
    return {
        "municipality": resolved_municipality,
        "question": chosen_question,
        "localities_detected": len(matches) - len(missing),
        "matches": matches,
        "by_level": {level: [item for item in matches if item["level"] == level] for level in levels},
        "georeferenced": len(representable_units),
        "summary": {
            "official_polygons": len(official_units),
            "analytical_sites": len(point_units),
            "review_required": len(review),
            "unmatched": len(unmatched),
            "missing_locality_records": int(sum(item["records"] for item in missing)),
            "representable_localities": len(representable_units),
            "representable_records": representable_records,
            "total_records": total_records,
            "record_coverage_percentage": round(representable_records / total_records * 100, 1)
            if total_records
            else 0.0,
        },
        "warnings": dataset.warnings,
    }


def _response_items(value: Any) -> list[str]:
    return [answer for answer in split_answers(value) if normalize_text(answer)]


def _features(
    locality_stats: dict[str, dict[str, Any]],
    municipality: str | None,
    repo: GeographyRepository,
    value_key: str,
    map_type: str,
):
    features: list[dict[str, Any]] = []
    nonmapped: list[dict[str, Any]] = []
    match_warnings: list[str] = []

    for locality, stats in locality_stats.items():
        if locality == MISSING_LOCALITY:
            nonmapped.append(
                {
                    "locality": locality,
                    "input_localities": [],
                    "matched_name": None,
                    "official_key": None,
                    "level": "MISSING_LOCALITY",
                    "confidence": 0.0,
                    "classification": "missing_locality",
                    "frequency": int(stats.get(value_key, stats.get("frequency", 0))),
                    "total_participations": int(stats.get("total_participations", 0)),
                    "warning": "El registro no contiene localidad o colonia.",
                }
            )
            continue
        match = repo.resolve(locality, municipality)
        if match.geometry is not None and match.level in ACCEPTED_POLYGON_LEVELS:
            if match.warning:
                match_warnings.append(f"{locality}: {match.warning}")
            properties = {
                **stats,
                "locality": match.matched_name or locality,
                "input_localities": stats.get("input_localities", [locality]),
                "match_level": match.level,
                "match_confidence": match.confidence,
                "match_warning": match.warning,
                "official_key": match.official_key,
                "value": int(stats.get(value_key, 0)),
                "map_type": map_type,
                "geometry_kind": match.geometry_kind or "locality",
                "official_boundary": True,
            }
            features.append({"type": "Feature", "geometry": match.geometry, "properties": properties})
            continue

        point_match = repo.resolve_point(locality, municipality)
        point_only = point_match.level in EXACT_POINT_LEVELS
        diagnostic = point_match if point_only or point_match.level == "POINT_FUZZY_HIGH" else match
        nonmapped.append(
            {
                "locality": locality,
                "input_localities": stats.get("input_localities", [locality]),
                "matched_name": diagnostic.matched_name,
                "official_key": diagnostic.official_key,
                "level": diagnostic.level,
                "confidence": diagnostic.confidence,
                "classification": "point_only" if point_only else "unmatched",
                "frequency": int(stats.get(value_key, stats.get("frequency", 0))),
                "total_participations": int(stats.get("total_participations", 0)),
                "warning": diagnostic.warning,
            }
        )

    return {"type": "FeatureCollection", "features": features}, nonmapped, match_warnings


def _merge_stats(current: dict[str, Any], values: dict[str, Any]) -> None:
    for key in ("frequency", "total_participations", "total_response_mentions"):
        if key in values:
            current[key] = int(current.get(key, 0)) + int(values.get(key, 0))
    current.setdefault("input_localities", [])
    current["input_localities"] = list(
        dict.fromkeys([*current["input_localities"], *values.get("input_localities", [])])
    )
    if values.get("answer_counts"):
        counts = current.setdefault("answer_counts", {})
        for answer, count in values["answer_counts"].items():
            counts[answer] = int(counts.get(answer, 0)) + int(count)
    if current.get("answer_counts"):
        dominant, dominant_count = max(current["answer_counts"].items(), key=lambda item: item[1])
        if current.get("dominant_answer") is not None:
            current["dominant_answer"] = dominant
            total = int(current.get("total_response_mentions", current.get("total_participations", 0)))
            current["frequency"] = int(dominant_count)
            current["percentage_locality"] = round(dominant_count / total * 100, 1) if total else 0
        elif "frequency" in current:
            total = int(current.get("total_response_mentions", current.get("total_participations", 0)))
            current["percentage_locality"] = round(int(current["frequency"]) / total * 100, 1) if total else 0
    elif "frequency" in current:
        total = int(current.get("total_participations", 0))
        current["percentage_locality"] = round(int(current["frequency"]) / total * 100, 1) if total else 0


def _canonicalize_stats(
    locality_stats: dict[str, dict[str, Any]],
    municipality: str | None,
    repo: GeographyRepository,
) -> dict[str, dict[str, Any]]:
    """Merge safe aliases that resolve to one official polygon or exact point."""
    canonical: dict[str, dict[str, Any]] = {}
    for locality, raw_values in locality_stats.items():
        values = {**raw_values, "input_localities": [locality]}
        if locality == MISSING_LOCALITY:
            canonical[locality] = values
            continue
        match = repo.resolve(locality, municipality)
        name = match.matched_name if match.geometry and match.level in ACCEPTED_POLYGON_LEVELS else None
        if not name:
            point_match = repo.resolve_point(locality, municipality)
            if point_match.level in EXACT_POINT_LEVELS:
                name = point_match.matched_name
        name = name or locality
        if name not in canonical:
            canonical[name] = values
        else:
            _merge_stats(canonical[name], values)
    return canonical


def _frequency_stats(frame: pd.DataFrame, locality_column: str, question: str, answer: str):
    normalized_answer = normalize_text(answer)
    locality_stats: dict[str, dict[str, Any]] = {}
    for locality, group in frame.groupby(locality_column, dropna=False):
        display = MISSING_LOCALITY if pd.isna(locality) or not str(locality).strip() else str(locality).strip()
        rows = [_response_items(value) for value in group[question].tolist()]
        total = sum(bool(items) for items in rows)
        if not total:
            continue
        counter = Counter(item for items in rows for item in items)
        total_response_mentions = int(sum(counter.values()))
        mentions = sum(normalized_answer in {normalize_text(item) for item in items} for items in rows)
        locality_stats[display] = {
            "frequency": int(mentions),
            "total_participations": int(total),
            "total_response_mentions": total_response_mentions,
            "percentage_locality": round(mentions / total_response_mentions * 100, 1)
            if total_response_mentions
            else 0.0,
            "answer_counts": dict(counter),
        }
    return locality_stats


def _composition_stats(frame: pd.DataFrame, locality_column: str, question: str):
    locality_stats: dict[str, dict[str, Any]] = {}
    for locality, group in frame.groupby(locality_column, dropna=False):
        display = MISSING_LOCALITY if pd.isna(locality) or not str(locality).strip() else str(locality).strip()
        rows = [_response_items(value) for value in group[question].tolist()]
        counter = Counter(item for items in rows for item in items)
        valid_rows = sum(bool(items) for items in rows)
        total_response_mentions = int(sum(counter.values()))
        if not counter or not valid_rows:
            continue
        locality_stats[display] = {
            "frequency": total_response_mentions,
            "total_participations": int(valid_rows),
            "total_response_mentions": total_response_mentions,
            "percentage_locality": 100.0,
            "answer_counts": dict(counter),
        }
    return locality_stats


def _response_categories(frame: pd.DataFrame, question: str) -> list[str]:
    return sorted(
        {
            answer.strip()
            for value in frame[question].dropna().tolist()
            for answer in _response_items(value)
        }
    )


def _analysis_sites(
    locality_stats: dict[str, dict[str, Any]],
    municipality: str | None,
    repo: GeographyRepository,
    value_key: str,
    map_type: str,
) -> list[dict[str, Any]]:
    """Attach aggregated survey values to trusted geographic sites.

    The sites are inputs for a client-side nearest-neighbour influence view;
    they are not asserted to be official locality boundaries.
    """
    sites: list[dict[str, Any]] = []
    for locality, stats in locality_stats.items():
        if locality == MISSING_LOCALITY:
            continue
        site = repo.analysis_site(locality, municipality)
        if not site:
            continue
        sites.append(
            {
                **site,
                **stats,
                "input_localities": stats.get("input_localities", [locality]),
                "value": int(stats.get(value_key, stats.get("frequency", 0))),
                "map_type": map_type,
            }
        )
    return sites


def _result(
    map_type: str,
    title: str,
    municipality: str | None,
    question: str | None,
    answer: str | None,
    stats: dict[str, dict[str, Any]],
    repo: GeographyRepository,
    value_key: str,
    warnings: list[str] | None = None,
):
    stats = _canonicalize_stats(stats, municipality, repo)
    feature_collection, nonmapped, match_warnings = _features(
        stats, municipality, repo, value_key, map_type
    )
    influence_sites = _analysis_sites(stats, municipality, repo, value_key, map_type)
    territory_background = repo.territory_background(municipality)
    ranking = sorted(
        ({"locality": locality, **values} for locality, values in stats.items()),
        key=lambda row: row.get(value_key, row.get("frequency", 0)),
        reverse=True,
    )
    for index, row in enumerate(ranking, 1):
        row["rank"] = index

    max_row = ranking[0] if ranking else None
    mapped_features = feature_collection["features"]
    mapped_records = sum(
        int(feature.get("properties", {}).get("total_participations", 0))
        for feature in mapped_features
    )
    mapped_mentions = sum(
        int(feature.get("properties", {}).get("value", 0)) for feature in mapped_features
    )
    point_only = [item for item in nonmapped if item["classification"] == "point_only"]
    unmatched = [item for item in nonmapped if item["classification"] == "unmatched"]
    missing_locality = [item for item in nonmapped if item["classification"] == "missing_locality"]
    point_only_records = sum(int(item.get("total_participations", 0)) for item in point_only)
    unmatched_records = sum(int(item.get("total_participations", 0)) for item in unmatched)
    missing_locality_records = sum(int(item.get("total_participations", 0)) for item in missing_locality)
    total_records = mapped_records + point_only_records + unmatched_records + missing_locality_records
    influence_records = sum(int(site.get("total_participations", 0)) for site in influence_sites)
    official_polygons = territory_background.get("features", [])
    mapped_keys = {
        feature.get("properties", {}).get("official_key")
        for feature in mapped_features
        if feature.get("properties", {}).get("official_key")
    }

    result_warnings = list(warnings or [])
    result_warnings.extend(match_warnings)
    if point_only:
        result_warnings.append(
            f"{len(point_only)} localidad(es) del Excel existen en el catálogo puntual de INEGI, pero no tienen polígono oficial; sólo se incluyen en la vista analítica de influencia."
        )
    if unmatched:
        result_warnings.append(
            f"{len(unmatched)} nombre(s) del Excel no se pudieron vincular con confianza a una unidad poligonal oficial."
        )
    if missing_locality_records:
        result_warnings.append(
            f"{missing_locality_records} registro(s) con respuesta no contienen localidad o colonia y no se pudieron representar."
        )
    result_warnings.append(
        "La cartografía oficial de localidades no forma una partición continua de todo el municipio: sólo se colorean localidades amanzanadas y asentamientos con polígono publicado por INEGI."
    )
    result_warnings.append(
        "La cobertura completa reparte el municipio por cercanía al sitio geográfico de cada localidad (vecino más cercano/Tobler); conserva las frecuencias del Excel, pero sus divisiones no son límites oficiales."
    )
    result_warnings = list(dict.fromkeys(result_warnings))

    metrics = {
        "mentions": int(sum(int(row.get(value_key, row.get("frequency", 0))) for row in ranking)),
        "mapped_mentions": mapped_mentions,
        "localities": len(stats),
        "max_frequency": int(max_row.get(value_key, max_row.get("frequency", 0))) if max_row else 0,
        "max_locality_responses": max(
            (int(row.get("total_participations", 0)) for row in ranking),
            default=0,
        ),
        "top_locality": max_row.get("locality") if max_row else None,
        "georeferenced_localities": len(mapped_features),
        "georeferenced_polygons": len(mapped_features),
        "official_territory_polygons": len(official_polygons),
        "official_polygons_without_excel_data": max(len(official_polygons) - len(mapped_keys), 0),
        "point_only_localities": len(point_only),
        "point_only_records": point_only_records,
        "unmatched_localities": len(unmatched),
        "unmatched_records": unmatched_records,
        "missing_locality_records": missing_locality_records,
        "mapped_records": mapped_records,
        "total_records_with_locality": total_records - missing_locality_records,
        "total_response_records": total_records,
        "record_coverage_percentage": round(mapped_records / total_records * 100, 1) if total_records else 0.0,
        "influence_site_localities": len(influence_sites),
        "influence_records": influence_records,
        "influence_coverage_percentage": round(influence_records / total_records * 100, 1) if total_records else 0.0,
        "coverage_mode": "official_polygons_and_analytical_influence",
    }
    result = MapResult(
        map_type,
        title,
        municipality,
        question,
        answer,
        metrics,
        ranking[:20],
        feature_collection,
        nonmapped,
        result_warnings,
    ).as_dict()
    result["background"] = repo.background(municipality)
    result["territory_background"] = territory_background
    result["influence_sites"] = influence_sites
    result["influence_model"] = {
        "method": "nearest_neighbour_voronoi",
        "principle": "Primera Ley de la Geografía de Tobler",
        "coverage": "municipality_clipped",
        "official_boundaries": False,
        "uses_observed_values_only": True,
    }
    result["cartography"] = {
        "state": "Hidalgo",
        "policy": "official_boundaries_with_optional_analytical_influence",
        "sources": ["INEGI Marco Geoestadístico", "INEGI DCAH"],
        "official_feature_collection_only": True,
        "analytical_influence_available": True,
        "limitation": "Las localidades rurales no amanzanadas publicadas únicamente como puntos no pueden representarse honestamente como coropletas.",
    }
    return result


def generate_frequency_map(
    file_path: str | None = None,
    file: Any = None,
    municipality: str | None = None,
    question: str | None = None,
    answer: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    if not answer:
        raise ValueError("Indica qué respuesta quieres representar.")
    dataset = load_dataset(file_path, file)
    frame, _, _, locality_column, chosen_question, resolved_municipality = _context(
        dataset, municipality, question
    )
    if not chosen_question:
        raise ValueError("Encontré varias preguntas posibles; elige una para continuar.")
    stats = _frequency_stats(frame, locality_column, chosen_question, answer)
    if not stats or not any(int(values.get("frequency", 0)) for values in stats.values()):
        raise ValueError("No encontré menciones de esa respuesta con los filtros indicados.")
    result = _result(
        "frequency",
        f"Frecuencia territorial — {answer}",
        resolved_municipality,
        chosen_question,
        answer,
        stats,
        _geography_repository(catalog_path),
        "frequency",
        list(dataset.warnings),
    )
    result["response_categories"] = _response_categories(frame, chosen_question)
    return result


def generate_composition_map(
    file_path: str | None = None,
    file: Any = None,
    municipality: str | None = None,
    question: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    """Generate one map whose territory fills encode every answer share."""
    dataset = load_dataset(file_path, file)
    frame, _, _, locality_column, chosen_question, resolved_municipality = _context(
        dataset, municipality, question
    )
    if not chosen_question:
        raise ValueError("Encontré varias preguntas posibles; elige una para continuar.")
    stats = _composition_stats(frame, locality_column, chosen_question)
    if not stats:
        raise ValueError("No encontré respuestas válidas para la pregunta seleccionada.")
    result = _result(
        "composition",
        "Distribución porcentual territorial",
        resolved_municipality,
        chosen_question,
        "Todas las respuestas",
        stats,
        _geography_repository(catalog_path),
        "total_participations",
        list(dataset.warnings),
    )
    result["response_categories"] = _response_categories(frame, chosen_question)
    result["composition_model"] = {
        "unit": "porcentaje de respuestas válidas dentro de cada localidad/colonia",
        "uses_observed_values_only": True,
        "multi_response_policy": "Cada opción marcada suma una mención; el denominador es el total de menciones de la localidad.",
    }
    result["metrics"]["response_mentions"] = int(
        sum(int(values.get("total_response_mentions", 0)) for values in stats.values())
    )
    return result


def generate_dominant_answer_map(
    file_path: str | None = None,
    file: Any = None,
    municipality: str | None = None,
    question: str | None = None,
    catalog_path: str | None = None,
    sheet_name: str | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(file_path, file, sheet_name=sheet_name)
    frame, _, _, locality_column, chosen_question, resolved_municipality = _context(
        dataset, municipality, question
    )
    if not chosen_question:
        raise ValueError("Encontré varias preguntas posibles; elige una para continuar.")
    response_categories = sorted(
        {
            answer.strip()
            for value in frame[chosen_question].dropna().tolist()
            for answer in _response_items(value)
        }
    )
    stats: dict[str, dict[str, Any]] = {}
    for locality, group in frame.groupby(locality_column, dropna=False):
        display = MISSING_LOCALITY if pd.isna(locality) or not str(locality).strip() else str(locality).strip()
        counter = Counter(
            answer
            for value in group[chosen_question].tolist()
            for answer in _response_items(value)
        )
        valid_rows = sum(bool(_response_items(value)) for value in group[chosen_question].tolist())
        if not counter or not valid_rows:
            continue
        dominant, count = counter.most_common(1)[0]
        stats[display] = {
            "dominant_answer": dominant,
            "frequency": int(count),
            "total_participations": int(valid_rows),
            "percentage_locality": round(count / valid_rows * 100, 1),
            "answer_counts": dict(counter),
        }
    repo = _geography_repository(catalog_path)
    result = _result(
        "dominant",
        "Respuesta predominante por localidad",
        resolved_municipality,
        chosen_question,
        "Todas las respuestas",
        stats,
        repo,
        "frequency",
        list(dataset.warnings),
    )
    result["response_categories"] = response_categories
    result["metrics"]["mentions"] = int(
        sum(int(values["total_participations"]) for values in stats.values())
    )
    return result


def generate_participation_map(
    file_path: str | None = None,
    file: Any = None,
    municipality: str | None = None,
    question: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(file_path, file)
    frame, _, _, locality_column, chosen_question, resolved_municipality = _context(
        dataset, municipality, question
    )
    stats: dict[str, dict[str, Any]] = {}
    for locality, group in frame.groupby(locality_column, dropna=False):
        display = MISSING_LOCALITY if pd.isna(locality) or not str(locality).strip() else str(locality).strip()
        frequency = (
            sum(bool(_response_items(value)) for value in group[chosen_question].tolist())
            if chosen_question
            else len(group)
        )
        if frequency:
            stats[display] = {
                "frequency": int(frequency),
                "total_participations": int(frequency),
                "percentage_locality": 100.0,
            }
    return _result(
        "participation",
        "Participación total por localidad",
        resolved_municipality,
        chosen_question,
        "Todas las respuestas",
        stats,
        _geography_repository(catalog_path),
        "frequency",
        list(dataset.warnings),
    )


def get_question_options(
    file_path: str | None = None,
    file: Any = None,
    question: str | None = None,
    intent: str | None = None,
    municipality: str | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(file_path, file)
    if municipality:
        frame, schema, _, _, chosen, resolved_municipality = _context(
            dataset, municipality, question
        )
    else:
        frame = _single_frame(dataset)
        schema = detect_schema(frame)
        chosen = choose_question(schema, question, intent)
        resolved_municipality = None
    if not chosen:
        return {
            "questions": schema["questions"],
            "options": [],
            "needs_selection": True,
            "message": "Elige una de las preguntas detectadas.",
        }
    detected_options = question_options(frame, chosen)
    valid_response_rows = int(sum(bool(_response_items(value)) for value in frame[chosen].tolist()))
    options = [{**option, "map_mode": "frequency"} for option in detected_options]
    return {
        "question": chosen,
        "options": options,
        "municipality": resolved_municipality,
        "response_summary": {
            "valid_rows": valid_response_rows,
            "categories": len(options),
            "mentions": int(sum(int(option.get("frequency", 0)) for option in options)),
        },
        "needs_selection": False,
        "warnings": dataset.warnings,
    }


def analyze_spatial_distribution(map_result: dict[str, Any]) -> dict[str, Any]:
    features = map_result.get("feature_collection", {}).get("features", [])
    ranking = map_result.get("ranking", [])
    top = [item.get("locality") for item in ranking[:3] if item.get("locality")]
    return {
        "summary": (
            f"La mayor frecuencia se encuentra en {', '.join(top)}."
            if top
            else "No hay localidades suficientes para resumir el patrón."
        ),
        "top_localities": top,
        "georeferenced_features": len(features),
        "unmatched_localities": map_result.get("unmatched_localities", []),
        "limitations": [
            "La descripción es asociativa y no implica causalidad.",
            "Los conteos provienen del Excel; la cobertura continua, si se usa, representa influencia analítica y no límites oficiales.",
        ],
    }


def create_map_from_intent(
    file_path: str | None = None,
    file: Any = None,
    intent: str | None = None,
    municipality: str | None = None,
    question: str | None = None,
    answer: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    """Infer the requested frequency map while preserving explicit selections."""
    intent_text = normalize_text(intent)
    if any(
        term in intent_text
        for term in ("todas las respuestas", "todas las opciones", "distribucion completa", "composicion porcentual", "composicion de respuestas")
    ):
        return generate_composition_map(
            file_path=file_path,
            file=file,
            municipality=municipality,
            question=question,
            catalog_path=catalog_path,
        )
    if any(
        term in intent_text
        for term in ("respuesta predominante", "respuesta principal", "que predomina")
    ):
        return generate_dominant_answer_map(
            file_path=file_path,
            file=file,
            municipality=municipality,
            question=question,
            catalog_path=catalog_path,
        )
    if any(
        term in intent_text
        for term in ("participacion total", "todas las participaciones", "total de registros")
    ):
        return generate_participation_map(
            file_path=file_path,
            file=file,
            municipality=municipality,
            question=question,
            catalog_path=catalog_path,
        )

    dataset = load_dataset(file_path, file)
    frame = _single_frame(dataset)
    schema = detect_schema(frame)
    chosen_question = choose_question(schema, question, intent)
    if not chosen_question:
        return {
            "needs_selection": True,
            "message": "Encontré varias preguntas posibles; elige una para continuar.",
            "questions": schema["questions"],
            "warnings": dataset.warnings,
        }

    options = question_options(frame, chosen_question)
    chosen_answer = answer
    if not chosen_answer:
        scored = []
        for option in options:
            option_text = normalize_text(option["answer"])
            tokens = [token for token in option_text.split() if len(token) > 2]
            score = sum(token in intent_text for token in tokens)
            if score:
                scored.append((score, int(option["frequency"]), option["answer"]))
        if scored:
            scored.sort(reverse=True)
            chosen_answer = scored[0][2]
    if not chosen_answer:
        return {
            "needs_selection": True,
            "message": "Indica qué respuesta quieres representar.",
            "question": chosen_question,
            "options": options,
            "warnings": dataset.warnings,
        }

    result = generate_frequency_map(
        file_path=file_path,
        file=file,
        municipality=municipality,
        question=chosen_question,
        answer=chosen_answer,
        catalog_path=catalog_path,
    )
    result["intent_route"] = "frequency"
    return result
