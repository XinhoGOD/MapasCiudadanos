from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

import pandas as pd

from app.models.domain import ColumnCandidate, SurveyQuestion
from app.normalization import normalize_text


def _column_kind(name: str) -> tuple[str, float, list[str]]:
    raw = str(name).strip()
    normalized = normalize_text(name)
    evidence: list[str] = []
    direct_municipality = any(term in normalized for term in ("municipio de residencia", "selecciona tu municipio", "municipio donde vive", "municipio donde reside"))
    direct_locality = any(term in normalized for term in ("localidad o colonia", "localidad de residencia", "colonia donde vive", "comunidad donde vive"))
    if not direct_municipality and not direct_locality and ("?" in raw or "¿" in raw):
        evidence.append("signo de pregunta")
        return "question", 0.95, evidence
    if not direct_municipality and not direct_locality and len(raw) >= 24:
        evidence.append("encabezado extenso")
        return "question", 0.72, evidence
    if any(term in normalized for term in ("municipio", "municipalidad", "ayuntamiento")):
        evidence.append("municipio")
        return "municipality", 0.98, evidence
    if any(term in normalized for term in ("localidad", "colonia", "comunidad", "barrio", "poblado", "ejido", "fraccionamiento")):
        evidence.append("localidad/colonia/comunidad")
        return "locality", 0.98, evidence
    if any(term in normalized for term in ("estado", "entidad federativa")):
        evidence.append("entidad")
        return "state", 0.9, evidence
    return "unknown", 0.0, evidence


def detect_schema(frame: pd.DataFrame) -> dict[str, object]:
    candidates: list[ColumnCandidate] = []
    questions: list[SurveyQuestion] = []
    for name in frame.columns:
        kind, confidence, evidence = _column_kind(str(name))
        series = frame[name].dropna().astype(str).map(str.strip)
        values = [value for value in series.tolist() if value]
        unique = list(dict.fromkeys(values))
        if kind == "unknown":
            normalized_name = normalize_text(name)
            likely_question = "?" in str(name) or len(str(name)) >= 24 or len(unique) <= 40
            confidence = 0.72 if likely_question else 0.35
            kind = "question" if likely_question else "other"
            if "?" in str(name):
                evidence.append("signo de pregunta")
            if len(unique) <= 40:
                evidence.append("respuesta categórica")
        candidates.append(ColumnCandidate(str(name), kind, confidence, evidence))
        if kind == "question":
            questions.append(SurveyQuestion(str(name), confidence, len(unique), unique[:8]))

    municipalities = [asdict(candidate) for candidate in candidates if candidate.kind == "municipality"]
    localities = [asdict(candidate) for candidate in candidates if candidate.kind == "locality"]
    states = [asdict(candidate) for candidate in candidates if candidate.kind == "state"]
    return {
        "columns": [asdict(candidate) for candidate in candidates],
        "municipality_candidates": municipalities,
        "locality_candidates": localities,
        "state_candidates": states,
        "questions": [asdict(question) for question in questions],
    }


def choose_column(schema: dict[str, object], kind: str, requested: str | None = None) -> str | None:
    if requested:
        for candidate in schema.get("columns", []):
            if candidate.get("name") == requested:
                return requested
    candidates = [candidate for candidate in schema.get("columns", []) if candidate.get("kind") == kind]
    return max(candidates, key=lambda candidate: float(candidate.get("confidence", 0))).get("name") if candidates else None


def choose_question(schema: dict[str, object], requested: str | None = None, intent: str | None = None) -> str | None:
    questions = [question.get("name") for question in schema.get("questions", [])]
    if requested and requested in questions:
        return requested
    if intent:
        normalized_intent = normalize_text(intent)
        scored = sorted(((sum(token in normalize_text(question) for token in normalized_intent.split()), question) for question in questions), reverse=True)
        if scored and scored[0][0] > 0:
            return scored[0][1]
    return questions[0] if len(questions) == 1 else None


def question_options(frame: pd.DataFrame, question: str) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    displays: dict[str, str] = {}
    for value in frame[question].dropna().tolist():
        for answer in str(value).split(","):
            answer = answer.strip()
            key = normalize_text(answer)
            if key:
                counts[key] = counts.get(key, 0) + 1
                displays.setdefault(key, answer)
    return [{"answer": displays[key], "frequency": counts[key]} for key in sorted(counts, key=counts.get, reverse=True)]
