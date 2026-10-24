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
    def score(self, suggestions: list[Suggestion],base:float| None = None) -> QualityScore:
        

        dimension_groups = self._group_by_dimension(suggestions)
        dimension_scores = {
            dim: self._score_dimension(dimension_groups[dim])
            for dim in DIMENSION_WEIGHTS
        }
        weighted_score = self._compute_weighted_scores(dimension_scores)
        grade = self._map_grade(weighted_score)
        check_passed = self._check_passed(suggestions)
        delta = self._compute_delta(weighted_score, base)
        summary = self._write_summary(suggestions,weighted_score)

        critical_count = sum(1 for s in suggestions if s.severity == "CRITICAL")
        high_count = sum(1 for s in suggestions if s.severity == "HIGH") 

        return QualityScore(
            overall_score=weighted_score,
            grade=grade,
            passed = check_passed,
            summary=summary,
            delta=delta,
            dimension_scores=dimension_scores,
            critical_count= critical_count,
            high_count=high_count
        )

        


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

    def _check_passed(self, suggestions: list[Suggestion]) -> bool:
        return not any(s.severity in BLOCK_SEVERITY for s in suggestions)

    def _compute_delta(self, overall:float, base:float) -> float:
        if base is None:
            return None
        return round(overall - base, 2)
    
    def _write_summary(self, suggestions: list[Suggestion], score: float) -> str:
        critical = sum(1 for s in suggestions if s.severity == "CRITICAL")
        high = sum(1 for s in suggestions if s.severity == "HIGH")

        if critical:
            return f"{critical} critical issue(s) found — review required"
        if high:
            return f"{high} high severity issue(s) found"
        if score >= 90:
            return "Clean pass — no significant issues"
        return f"{len(suggestions)} issue(s) found across multiple files"
