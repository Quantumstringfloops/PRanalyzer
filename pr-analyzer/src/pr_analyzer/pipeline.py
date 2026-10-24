import time
import structlog
from pr_analyzer.models import PRSummary, Suggestion ,AnalysisResult, QualityScore
from pr_analyzer.github.client import GithubClient
from pr_analyzer.analyzer.ast_analyzer import StaticAnalyzer
from pr_analyzer.security.scanner import SecurityScanner
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.analyzer.scorer import Scorer
from pr_analyzer.config import settings

log = structlog.get_logger(__name__)
class AnalysisPipeline:
    def __init__(self):
            self.github   = GithubClient()
            self.static   = StaticAnalyzer()
            self.security = SecurityScanner()
            self.llm      = LLMEngine()
            self.scorer   = Scorer()

    def run(self, url: str, skip_llm: bool = False) -> AnalysisResult:
        start = time.time()


        log.info("Fetching PR")
        pr = self.github.fetch_pr(url)
        log.info("PR fetched succesfully", PR = pr)

        log.info("Static analysis starting..")
        static_analysis = self.static.analyze(pr.files)

        log.info("Security analysis starting..")
        security_analysis = self.security.scan(pr.files)

        existing_findings = static_analysis + security_analysis
        log.info("LLM analysis ..")

        llm_suggestions = []
        if not skip_llm:
            log.info("running LLM analysis")
            llm_suggestions = self.llm.analyze(pr, existing_findings)
            log.info("LLM analysis done", count=len(llm_suggestions))
        else:
            log.info("skipping LLM analysis")


    def run_and_post(self, url: str, skip_llm: bool = False) -> AnalysisResult:
        pass

    def _post_github_review(self, result: AnalysisResult) -> None:
        pass

    def _map_suggestions_to_positions(self, result: AnalysisResult) -> list[tuple]:
        pass

    def _build_review_body(self, result: AnalysisResult) -> str:
        pass