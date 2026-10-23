import structlog
from pr_analyzer.models import QualityScore, AnalysisResult,Suggestion,PRSummary
from pr_analyzer.config import settings
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

    def _group_by_dimension(self, suggestions: list[Suggestion]) -> dict:
        groups = {}

        for dim in DIMENSION_WEIGHTS:
            groups[dim] = []

        for s in suggestions:
            if s.category in groups:
                groups[s.category].append(s)

        return groups

    def _score_dimension(self, suggestions: list[Suggestion]) -> float:
        score = 100
        for s in suggestions:
            penalty = SEVERITY_PENALTIES[s.severity]

            score -= penalty * s.confidence
        if score < 0: return 0
        return score

    def _compute_weighted_scores(self, dimension_scores:dict)-> float:
        weighted_scores = sum(dimension_scores[dim]* DIMENSION_WEIGHTS[dim] for dim in DIMENSION_WEIGHTS)
        return weighted_scores

    def _map_grade(self, score: float) -> str:
        for minimum, grade in GRADE_THRESHOLDS:
            if score >= minimum:
                return grade
        return "F"
