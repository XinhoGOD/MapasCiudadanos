import csv
import json
from pathlib import Path

import pandas as pd

from app.analytics import (
    create_map_from_intent,
    generate_dominant_answer_map,
    generate_frequency_map,
    generate_composition_map,
    generate_participation_map,
    get_question_options,
    inspect_dataset,
    resolve_geography,
)
from app.geography import GeographyRepository


def write_fixture(tmp_path):
    survey = tmp_path / "encuesta.csv"
    with survey.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Municipio de residencia", "Localidad o colonia", "¿Qué servicio considera prioritario?"])
        writer.writerow(["Atotonilco de Tula", "Vito", "Agua potable"])
        writer.writerow(["Atotonilco de Tula", "Vito", "Agua potable"])
        writer.writerow(["Atotonilco de Tula", "Boxfi", "Pavimentación"])
        writer.writerow(["Tula", "Vito", "Agua potable"])
    catalog = tmp_path / "catalog.csv"
    with catalog.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["locality", "municipality", "latitude", "longitude", "CVE_LOC", "CVE_MUN"])
        writer.writeheader()
        writer.writerow({"locality":"Vito", "municipality":"Atotonilco de Tula", "latitude":"20.0", "longitude":"-99.2", "CVE_LOC":"001", "CVE_MUN":"013"})
        writer.writerow({"locality":"Boxfi", "municipality":"Atotonilco de Tula", "latitude":"20.1", "longitude":"-99.3", "CVE_LOC":"002", "CVE_MUN":"013"})
    geometry_dir = tmp_path / "geography" / "hidalgo"
    geometry_dir.mkdir(parents=True)
    geometry = {
        "type": "FeatureCollection",
        "metadata": {"CVE_MUN":"013"},
        "municipality": {"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-99.4,19.9],[-99.1,19.9],[-99.1,20.2],[-99.4,20.2],[-99.4,19.9]]]},"properties":{"municipality":"Atotonilco de Tula","CVE_MUN":"013"}},
        "features": [
            {"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-99.25,19.98],[-99.15,19.98],[-99.15,20.05],[-99.25,20.05],[-99.25,19.98]]]},"properties":{"locality":"Vito","official_key":"001","CVE_MUN":"013","geometry_kind":"locality"}},
            {"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-99.35,20.04],[-99.28,20.04],[-99.28,20.10],[-99.35,20.10],[-99.35,20.04]]]},"properties":{"locality":"Boxfi","official_key":"002","CVE_MUN":"013","geometry_kind":"locality"}},
        ],
    }
    (geometry_dir / "013.geojson").write_text(json.dumps(geometry), encoding="utf-8")
    return survey, catalog


def test_inspection_detects_semantics(tmp_path):
    survey, _ = write_fixture(tmp_path)
    inspection = inspect_dataset(file_path=str(survey))
    assert inspection["records"] == 4
    assert inspection["schema"]["municipality_candidates"]
    assert inspection["schema"]["locality_candidates"]
    assert inspection["schema"]["questions"]


def test_question_that_mentions_municipality_is_not_classified_as_location(tmp_path):
    survey = tmp_path / "pacula.csv"
    question = "En términos de seguridad pública, ¿cómo se siente al transitar por su municipio en comparación con el año anterior?"
    with survey.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["*Selecciona tu municipio de residencia*", "*¿En qué localidad o colonia vives?*", question])
        writer.writerow(["Pacula", "Jiliapan", "Más seguro"])
    inspection = inspect_dataset(file_path=str(survey))
    assert inspection["schema"]["questions"][0]["name"] == question
    assert inspection["schema"]["municipality_candidates"][0]["name"] == "*Selecciona tu municipio de residencia*"


def test_frequency_filters_municipality_and_answer(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    result = generate_frequency_map(file_path=str(survey), municipality="Atotonilco de Tula", answer="Agua potable", catalog_path=str(catalog))
    assert result["metrics"]["mentions"] == 2
    assert result["metrics"]["top_locality"] == "Vito"
    assert len(result["feature_collection"]["features"]) == 2
    values = {feature["properties"]["locality"]: feature["properties"]["frequency"] for feature in result["feature_collection"]["features"]}
    assert values == {"Boxfi": 0, "Vito": 2}
    assert all(feature["geometry"]["type"] == "Polygon" for feature in result["feature_collection"]["features"])
    assert result["background"]["metadata"]["CVE_MUN"] == "013"
    assert result["metrics"]["coverage_mode"] == "official_polygons_and_analytical_influence"
    assert result["metrics"]["influence_site_localities"] == 2
    assert result["metrics"]["influence_records"] == 3
    assert result["influence_model"]["official_boundaries"] is False
    assert "estimated_collection" not in result


def test_composition_map_groups_all_answers_inside_each_territory(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    result = generate_composition_map(
        file_path=str(survey),
        municipality="Atotonilco de Tula",
        catalog_path=str(catalog),
    )
    assert result["map_type"] == "composition"
    assert result["answer"] == "Todas las respuestas"
    assert result["response_categories"][0] == "Agua potable"
    assert len(result["response_categories"]) == 2
    pavement_answer = result["response_categories"][1]
    assert pavement_answer.startswith("Pavimentaci")
    counts = {
        feature["properties"]["locality"]: feature["properties"]["answer_counts"]
        for feature in result["feature_collection"]["features"]
    }
    assert counts == {"Boxfi": {pavement_answer: 1}, "Vito": {"Agua potable": 2}}
    assert result["metrics"]["response_mentions"] == 3


def test_intent_for_all_answers_routes_to_percentage_composition(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    result = create_map_from_intent(
        file_path=str(survey),
        municipality="Atotonilco de Tula",
        question="¿Qué servicio considera prioritario?",
        intent="Muéstrame todas las respuestas por localidad",
        catalog_path=str(catalog),
    )
    assert result["map_type"] == "composition"
    assert result["composition_model"]["uses_observed_values_only"] is True


def test_question_options_are_real_answers_filtered_by_municipality(tmp_path):
    survey, _ = write_fixture(tmp_path)
    options = get_question_options(file_path=str(survey), municipality="Atotonilco de Tula")
    assert options["options"] == [
        {"answer": "Agua potable", "frequency": 2, "map_mode": "frequency"},
        {"answer": "Pavimentación", "frequency": 1, "map_mode": "frequency"},
    ]
    assert options["response_summary"] == {"valid_rows": 3, "categories": 2, "mentions": 3}
    assert all(option["answer"] != "Todas las respuestas" for option in options["options"])


def test_geography_validation_previews_safe_coverage(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    validation = resolve_geography(
        file_path=str(survey),
        municipality="Atotonilco de Tula",
        question="¿Qué servicio considera prioritario?",
        catalog_path=str(catalog),
    )
    assert validation["summary"]["official_polygons"] == 2
    assert validation["summary"]["analytical_sites"] == 0
    assert validation["summary"]["representable_records"] == 3
    assert validation["summary"]["record_coverage_percentage"] == 100.0


def test_dominant_map_uses_official_locality_polygons_only(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    result = generate_dominant_answer_map(file_path=str(survey), municipality="Atotonilco de Tula", catalog_path=str(catalog))
    assert "surface_collection" not in result
    assert "estimated_collection" not in result
    assert result["title"] == "Respuesta predominante por localidad"
    assert len(result["territory_background"]["features"]) == 2
    counts = {
        feature["properties"]["locality"]: feature["properties"]["answer_counts"]
        for feature in result["feature_collection"]["features"]
    }
    assert counts == {"Boxfi": {"Pavimentación": 1}, "Vito": {"Agua potable": 2}}
    assert sum(site["total_participations"] for site in result["influence_sites"]) == 3


def test_projected_settlement_polygon_is_normalized_for_map():
    import json
    payload = json.loads(Path("data/geography/hidalgo/047.geojson").read_text(encoding="utf-8"))
    geometry = next(feature["geometry"] for feature in payload["features"] if feature["properties"]["locality"] == "CENTRO")
    point = GeographyRepository._normalize_geometry(geometry)["coordinates"][0][0]
    assert -180 <= point[0] <= 180
    assert -90 <= point[1] <= 90


def test_pacula_merges_official_locality_and_dcah_polygons():
    repo = GeographyRepository()
    centro = repo.resolve("Centro", "Pacula")
    tablon = repo.resolve("Tablón", "Pacula")
    assert centro.geometry is not None
    assert centro.level == "NORMALIZED_EXACT"
    assert tablon.geometry is not None
    assert tablon.level == "NORMALIZED_EXACT"
    assert centro.official_key == "1304700010001"


def test_pacula_state_suffix_is_a_conservative_alias():
    match = GeographyRepository().resolve("Pacula Hidalgo", "Pacula")
    assert match.matched_name == "Pacula"
    assert match.level == "ALIAS"
    assert match.geometry is not None


def test_juarez_hidalgo_abbreviated_locality_names_resolve_by_official_components():
    repo = GeographyRepository()
    expected = {
        "Barrio Bajío": "EL BAJÍO",
        "Barrio El Centro": "CENTRO",
        "Itztacoyotla": "San Lorenzo Itztacoyotla",
        "SAN LORENZO": "San Lorenzo Itztacoyotla",
        "San Nicolas": "San Nicolás Coatzontla",
        "Barrio Calvario San Lorenzo Itztacoyotla": "San Lorenzo Itztacoyotla",
    }
    for incoming, official in expected.items():
        match = repo.resolve(incoming, "Juárez Hidalgo")
        assert match.matched_name == official, incoming
        assert match.level in {"NORMALIZED_EXACT", "ALIAS"}, incoming
        assert match.geometry is not None, incoming

    assert repo.resolve("Barrio Cerro Verde", "Juárez Hidalgo").level == "UNMATCHED"


def test_official_points_are_reported_but_never_converted_to_polygons(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    geometry_path = tmp_path / "geography" / "hidalgo" / "013.geojson"
    payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    payload["features"] = [feature for feature in payload["features"] if feature["properties"]["locality"] == "Vito"]
    geometry_path.write_text(json.dumps(payload), encoding="utf-8")
    result = generate_participation_map(file_path=str(survey), municipality="Atotonilco de Tula", catalog_path=str(catalog))
    assert [feature["properties"]["locality"] for feature in result["feature_collection"]["features"]] == ["Vito"]
    boxfi = next(item for item in result["unmatched_localities"] if item["locality"] == "Boxfi")
    assert boxfi["classification"] == "point_only"
    assert boxfi["level"] == "POINT_EXACT"
    assert result["metrics"]["point_only_localities"] == 1
    assert {site["locality"] for site in result["influence_sites"]} == {"Vito", "Boxfi"}
    assert "estimated_collection" not in result


def test_geography_normalized_match(tmp_path):
    _, catalog = write_fixture(tmp_path)
    repo = GeographyRepository(str(catalog))
    match = repo.resolve(" vito ", "atotonilco de tula")
    assert match.level == "NORMALIZED_EXACT"
    assert match.official_key == "001"


def test_geography_typo_and_colonia_prefix_match_automatically(tmp_path):
    _, catalog = write_fixture(tmp_path)
    repo = GeographyRepository(str(catalog))

    accent_and_case = repo.resolve("VITÓ", "ATOTONILCO DE TULA")
    assert accent_and_case.matched_name == "Vito"
    assert accent_and_case.level == "NORMALIZED_EXACT"

    prefix_variant = repo.resolve("Col. Vito", "Atotonilco de Tula")
    assert prefix_variant.matched_name == "Vito"
    assert prefix_variant.level == "ALIAS"

    typo = repo.resolve("Boxfii", "Atotonilco de Tula")
    assert typo.matched_name == "Boxfi"
    assert typo.level == "FUZZY_HIGH"


def test_frequency_groups_spelling_variants_before_counting(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    with survey.open("a", encoding="utf-8", newline="") as handle:
        handle.write("Atotonilco de Tula,VITÓ,Agua potable\n")
        handle.write("Atotonilco de Tula,Col. Vito,Agua potable\n")
        handle.write("Atotonilco de Tula,Boxfii,Pavimentación\n")

    result = generate_frequency_map(
        file_path=str(survey),
        municipality="Atotonilco de Tula",
        answer="Agua potable",
        catalog_path=str(catalog),
    )
    values = {
        feature["properties"]["locality"]: feature["properties"]["frequency"]
        for feature in result["feature_collection"]["features"]
    }
    assert values["Vito"] == 4
    assert values["Boxfi"] == 0
    assert result["metrics"]["unmatched_localities"] == 0


def test_unmatched_and_null_localities_are_reported(tmp_path):
    survey, catalog = write_fixture(tmp_path)
    with survey.open("a", encoding="utf-8", newline="") as handle:
        handle.write("Atotonilco de Tula,,Agua potable\n")
        handle.write("Atotonilco de Tula,Localidad Desconocida,Agua potable\n")
    result = generate_frequency_map(file_path=str(survey), municipality="Atotonilco de Tula", answer="Agua potable", catalog_path=str(catalog))
    assert result["metrics"]["mentions"] == 4
    assert result["metrics"]["unmatched_localities"] == 1
    assert result["metrics"]["missing_locality_records"] == 1
    assert result["metrics"]["mapped_mentions"] == 2


def test_xlsx_with_multiple_sheets_is_read(tmp_path):
    workbook = tmp_path / "encuesta.xlsx"
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"Municipio": ["A"], "Localidad": ["Centro"], "Pregunta": ["Sí"]}).to_excel(writer, sheet_name="Respuestas", index=False)
        pd.DataFrame({"nota": ["hoja auxiliar"]}).to_excel(writer, sheet_name="Notas", index=False)
    inspection = inspect_dataset(file_path=str(workbook))
    assert {sheet["name"] for sheet in inspection["sheets"]} == {"Respuestas", "Notas"}
    selected = inspect_dataset(file_path=str(workbook), sheet_name="Respuestas")
    assert selected["sheets"] == [{"name": "Respuestas", "records": 1, "columns": ["Municipio", "Localidad", "Pregunta"]}]


def test_duplicate_headers_are_made_unique_before_schema_detection(tmp_path):
    import pandas as pd

    source = tmp_path / "duplicated.csv"
    pd.DataFrame(
        [["Municipio", "Municipio", "Localidad", "Pregunta"], ["Pacula", "Pacula", "Centro", "Sí"]]
    ).to_csv(source, index=False, header=False)

    inspection = inspect_dataset(file_path=str(source))

    assert "Municipio (2)" in inspection["schema"]["columns"][1]["name"]


def test_xlsx_formulas_are_reported_and_never_executed(tmp_path):
    from openpyxl import Workbook

    workbook_path = tmp_path / "formulas.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Municipio", "Localidad", "Pregunta"])
    sheet.append(["Pacula", '=IFERROR("Jiliapan","Jiliapan")', "Más seguro/a"])
    workbook.save(workbook_path)
    inspection = inspect_dataset(file_path=str(workbook_path))
    assert any("1 celda(s) con fórmula" in warning for warning in inspection["warnings"])


def test_pacula_backend_contains_only_official_polygon_units():
    repo = GeographyRepository()
    territory = repo.territory_background("Pacula")
    assert len(territory["features"]) == 18
    assert territory["metadata"]["official_only"] is True
    assert all(feature["geometry"]["type"] in {"Polygon", "MultiPolygon"} for feature in territory["features"])
    assert {feature["properties"]["geometry_kind"] for feature in territory["features"]} == {"locality", "settlement"}
