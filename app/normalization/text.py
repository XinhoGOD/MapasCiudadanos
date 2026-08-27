from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_CONTROL = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")

# These are intentionally conservative. They cover common ways survey
# respondents write a locality without requiring a hand-maintained list of
# every locality in Hidalgo.
_GEOGRAPHIC_TOKEN_ALIASES = {
    "col": "colonia",
    "colonia": "colonia",
    "fracc": "fraccionamiento",
    "fraccionamiento": "fraccionamiento",
    "sta": "santa",
    "snta": "santa",
    "sto": "santo",
    "snto": "santo",
}
_GEOGRAPHIC_LABELS = {
    "colonia",
    "fraccionamiento",
    "barrio",
    "comunidad",
    "localidad",
    "rancho",
    "ejido",
    "pueblo",
    "paraje",
}
_GEOGRAPHIC_STOPWORDS = _GEOGRAPHIC_LABELS | {
    "el",
    "la",
    "los",
    "las",
    "de",
    "del",
}


def normalize_text(value: object) -> str:
    """Normalize a value for matching while never changing the display value."""
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _CONTROL.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_geography_name(value: object) -> str:
    """Return a comparison key for locality names.

    This is separate from the display value and from the original survey. It
    handles the harmless textual differences commonly introduced in Excel:
    casing, accents, punctuation, separators, and repeated whitespace. It
    also expands a small set of unambiguous geographic abbreviations.
    """
    if value is None:
        return ""
    # A parenthetical qualifier is retained by normalize_text, but we expose a
    # second variant below so names such as ``Centro (cabecera)`` can match
    # the official ``Centro`` entry when that reduction is unique.
    raw = _PARENTHETICAL.sub(" ", str(value).replace("&", " y "))
    normalized = normalize_text(raw).replace("_", " ")
    tokens = normalized.split()
    expanded = [_GEOGRAPHIC_TOKEN_ALIASES.get(token, token) for token in tokens]
    return " ".join(expanded)


def geography_name_variants(value: object) -> list[str]:
    """Build safe comparison keys for an incoming locality name.

    The first key is the most faithful key. Additional keys only remove a
    generic geographic label (for example ``Col.``) or a parenthetical
    qualifier. The repository still checks uniqueness and municipality before
    accepting one of these variants.
    """
    if value is None:
        return []
    original = str(value)
    base = normalize_geography_name(original)
    if not base:
        return []

    variants = [base]
    full_normalized = normalize_text(original).replace("_", " ")
    if full_normalized and full_normalized not in variants:
        variants.append(full_normalized)

    tokens = base.split()
    if len(tokens) > 1 and tokens[0] in _GEOGRAPHIC_LABELS:
        without_label = " ".join(tokens[1:]).strip()
        if without_label and without_label not in variants:
            variants.append(without_label)

    # A respondent may write ``Barrio Bajío`` while the official record is
    # ``El Bajío``. This key is only an additional candidate; the repository
    # still requires a unique match inside the selected municipality.
    meaningful_tokens = [token for token in tokens if token not in _GEOGRAPHIC_STOPWORDS]
    compact = " ".join(meaningful_tokens).strip()
    if compact and compact not in variants:
        variants.append(compact)

    return list(dict.fromkeys(variant for variant in variants if variant))


def similarity(left: object, right: object) -> float:
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def split_answers(value: object) -> list[str]:
    """Split common multi-select separators without altering original labels."""
    if value is None:
        return []
    return [part.strip() for part in re.split(r"[,;|\n]+", str(value)) if part.strip()]
