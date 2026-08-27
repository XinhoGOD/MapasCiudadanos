from app.normalization import geography_name_variants, normalize_geography_name, normalize_text


def test_normalize_geographic_variants():
    assert normalize_text(" ACAXOCHITLÁN ") == "acaxochitlan"
    assert normalize_text("Acaxochitlan") == normalize_text("acaxochitlán")


def test_geography_name_variants_handle_labels_abbreviations_and_qualifiers():
    assert normalize_geography_name("  COL. San José  ") == "colonia san jose"
    assert "san jose" in geography_name_variants("  COL. San José  ")
    assert "centro" in geography_name_variants("Centro (cabecera)")
