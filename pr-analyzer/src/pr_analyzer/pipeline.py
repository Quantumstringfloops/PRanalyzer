import time
from dataclasses import dataclass
from pr_analyzer.models import PRSummary, Suggestion
from pr_analyzer.scorer import QualityScore

@dataclass
class AnalysisResult:
    pr:          PRSummary
    suggestions: list[Suggestion]
    score:       QualityScore
    duration:    float

class AnalysisPipeline:

    def run(self, url: str, skip_llm: bool = False) -> AnalysisResult:
        pass

    def run_and_post(self, url: str, skip_llm: bool = False) -> AnalysisResult:
        pass

    def _post_github_review(self, result: AnalysisResult) -> None:
        pass

    def _map_suggestions_to_positions(self, result: AnalysisResult) -> list[tuple]:
        pass

    def _build_review_body(self, result: AnalysisResult) -> str:
        pass