from scripts.preview_map import build_page


def test_preview_starts_without_a_preloaded_municipality():
    page = build_page()

    assert "window.openai.toolOutput = null;" in page
    assert "PACULA PRUEBA" not in page
    assert "Mapeo territorial de participación ciudadana" in page
    assert "Dirección de Participación Ciudadana" in page
    assert "Seleccionar Excel o CSV" in page
    assert "generate_composition_map" in page
    assert "Todas las respuestas · composición (%)" in page
    assert "Una respuesta ·" not in page
    assert "Medida individual" not in page
    assert "let coverageMode = 'official';" in page
    assert "osmRoadsSvg" in page
    assert "toggle-osm" in page
    assert "OpenStreetMap contributors" in page
