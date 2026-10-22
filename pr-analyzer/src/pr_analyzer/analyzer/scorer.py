import structlog
from pr_analyzer.models import QualityScore, AnalysisResult,Suggestion,PRSummary

log = structlog.get_logger(__name__)

SEVERITY_PENALTIES = {
    "CRITICAL": 40,
    "HIGH": 20,
    "MEDIUM": 8,
    "LOW" : 3,
}

DIMENSION_WEIGHTS = {
    "security" : 0.35,
    "style" : 0.15,
    "complexity" : 0.25,
    "performance" : 0.25,
    "documentation" : 0.15,
    "testing" : 0.10,
}


GRADE_THRESHOLDS = [
    (97, "A+"), (93, "A"), (90, "A-"),
    (87, "B+"), (83, "B"), (80, "B-"),
    (77, "C+"), (73, "C"), (70, "C-"),
    (65, "D"),  (0,  "F"),
]

BLOCK_SEVERITY = {"CRITICAL", "HIGH"}


class Scorer:
    def score(self, suggestions: list[Suggestion], pr:PRSummary,base_score:float| None = None) -> QualityScore:

    def _group_by_dimension(suggestions: list[Suggestion]) -> dict:
        groups = {}

        for dim in DIMENSION_WEIGHTS:
            groups[dim] = []

        for s in suggestions:
            if s.category in groups:
                groups[s.category].append(s)

        return groups