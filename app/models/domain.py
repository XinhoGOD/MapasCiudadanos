from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MapMode = Literal["frequency", "composition", "dominant", "participation"]
MatchLevel = Literal[
    "EXACT",
    "NORMALIZED_EXACT",
    "ALIAS",
    "FUZZY_HIGH",
    "FUZZY_REVIEW",
    "POINT_EXACT",
    "POINT_ALIAS",
    "POINT_FUZZY_HIGH",
    "UNMATCHED",
]


@dataclass(frozen=True)
class SurveyQuestion:
    name: str
    confidence: float
    unique_values: int
    sample_answers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ColumnCandidate:
    name: str
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeographyMatch:
    input_name: str
    matched_name: str | None
    level: MatchLevel
    confidence: float
    official_key: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    warning: str | None = None
    geometry: dict[str, Any] | None = None
    geometry_kind: str | None = None


@dataclass
class MapResult:
    map_type: MapMode
    title: str
    municipality: str | None
    question: str | None
    answer: str | None
    metrics: dict[str, Any]
    ranking: list[dict[str, Any]]
    feature_collection: dict[str, Any]
    unmatched_localities: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "map_type": self.map_type,
            "title": self.title,
            "municipality": self.municipality,
            "question": self.question,
            "answer": self.answer,
            "metrics": self.metrics,
            "ranking": self.ranking,
            "feature_collection": self.feature_collection,
            "unmatched_localities": self.unmatched_localities,
            "warnings": self.warnings,
        }
