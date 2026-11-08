"""Running pr-analyzer for the app, and gathering what the decision needs beyond its result:
every file GitHub lists for the PR (pr-analyzer drops the ones without a diff), the stages the
project switched off, and the scanners missing here."""
from typing import Protocol

import requests
import structlog
from github import GithubException
from pr_analyzer.llm.engine import LLM_RULE
from pr_analyzer.models import PRSummary
from pr_analyzer.pipeline import AnalysisPipeline
from pr_analyzer.project_config import CONFIG_FILE, read_project_config
from pr_analyzer.security import tools
from pr_analyzer.security.dep_auditor import DEPENDENCY_AUDIT_RULE
from pr_analyzer.security.semgrep_runner import SEMGREP_RULE

from review_app.models import AnalysisOutcome, ChangedFile, PullInfo

log = structlog.get_logger(__name__)

SCANNERS = ("semgrep", "pip-audit")
STAGES = (SEMGREP_RULE, DEPENDENCY_AUDIT_RULE, LLM_RULE)


class Analyzer(Protocol):
    def analyze(self, pull: PullInfo, *, token: str | None, post_review: bool,
                skip_llm: bool) -> AnalysisOutcome: ...


class PrAnalyzer:
    """pr-analyzer's AnalysisPipeline, run with the installation's token."""

    def analyze(self, pull: PullInfo, *, token: str | None, post_review: bool,
                skip_llm: bool) -> AnalysisOutcome:
        missing = tuple(tool for tool in SCANNERS if tools.find_tool(tool) is None)
        pipeline = AnalysisPipeline(skip_llm=skip_llm, post_review=post_review, github_token=token)
        try:
            result = pipeline.run(pull.url)
            changed, head = self._changed_files(pipeline, pull)
            disabled = self._disabled_stages(pipeline, result.pr)
        finally:
            pipeline.close()
        return AnalysisOutcome(
            result=result,
            changed_files=changed,
            disabled_stages=disabled,
            missing_tools=missing,
            head_moved=head is not None and head != result.pr.head_sha,
        )

    @staticmethod
    def _changed_files(pipeline: AnalysisPipeline,
                       pull: PullInfo) -> tuple[tuple[ChangedFile, ...] | None, str | None]:
        try:
            pr = pipeline.github.git_instance.get_repo(pull.repo).get_pull(pull.number)
            files = tuple(
                ChangedFile(filename=f.filename, status=f.status, changes=f.changes,
                            has_patch=f.patch is not None,
                            previous_filename=getattr(f, "previous_filename", None))
                for f in pr.get_files()
            )
            return files, pr.head.sha
        except (GithubException, requests.exceptions.RequestException) as e:
            # unknown counts as incomplete (decision.completeness_problems), never as complete
            log.warning("changed_files_unavailable", repo=pull.repo, pr=pull.number, error=str(e))
            return None, None

    @staticmethod
    def _disabled_stages(pipeline: AnalysisPipeline, pr: PRSummary) -> tuple[str, ...]:
        # the same file pr-analyzer read: .pr-analyzer.toml on the PR's base branch
        raw = pipeline.github.fetch_file(pr.repo, CONFIG_FILE, pr.base_branch)
        if raw is None:
            return ()
        config, problems = read_project_config(raw)
        if problems:
            return ()   # pr-analyzer used the defaults, and warned
        return tuple(stage for stage in STAGES if not config.enabled(stage))
