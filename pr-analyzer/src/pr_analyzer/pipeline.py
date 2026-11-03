import time
from collections import Counter
from dataclasses import replace

import structlog

from pr_analyzer.analyzer.ast_analyzer import StaticAnalyzer
from pr_analyzer.analyzer.scorer import Scorer
from pr_analyzer.config import settings
from pr_analyzer.diff import commentable_lines
from pr_analyzer.exceptions import PRAnalyzerError
from pr_analyzer.github.client import GithubClient
from pr_analyzer.llm.engine import LLM_RULE, LLMEngine
from pr_analyzer.models import AnalysisResult, FileChange, PRSummary, Suggestion
from pr_analyzer.project_config import (
    CONFIG_FILE,
    ProjectConfig,
    parse_project_config,
    severity_rank,
)
from pr_analyzer.security.scanner import SecurityScanner
from pr_analyzer.suppression import ignored_warning, split_ignored

log = structlog.get_logger(__name__)

# findings without an inline comment that are still listed in the review body
MAX_LISTED_IN_REVIEW = 20
# files named in the warning about ignore_paths; any beyond this are only counted
MAX_LISTED_SKIPPED = 10


def gets_inline_comment(s: Suggestion, min_severity: str = "MEDIUM") -> bool:
    # LOW findings are only counted in the review body by default: a comment on every one of
    # them buried the findings that matter. A project can move the line in .pr-analyzer.toml.
    return severity_rank(s.severity) >= severity_rank(min_severity)


def skipped_files_warning(skipped: list[FileChange]) -> str:
    names = ", ".join(f.filename for f in skipped[:MAX_LISTED_SKIPPED])
    if len(skipped) > MAX_LISTED_SKIPPED:
        names += f" and {len(skipped) - MAX_LISTED_SKIPPED} more"
    return f"Skipped {len(skipped)} file(s) matching ignore_paths in {CONFIG_FILE}: {names}"


def findings_off_the_diff(findings: list[Suggestion]) -> list[str]:
    # review-body lines for findings that should have had an inline comment but whose line
    # isn't part of the diff
    if not findings:
        return []
    lines = ["", f"**Findings not attached to a diff line ({len(findings)}):**"]
    for s in findings[:MAX_LISTED_IN_REVIEW]:
        where = f"{s.filename}:{s.line}" if s.line else s.filename
        lines.append(f"- **{s.severity}** `{where}` — {s.title}")
    if len(findings) > MAX_LISTED_IN_REVIEW:
        lines.append(f"- … and {len(findings) - MAX_LISTED_IN_REVIEW} more")
    return lines


def low_severity_counts(findings: list[Suggestion], min_severity: str = "MEDIUM") -> list[str]:
    # findings below the inline-comment threshold: LOW ones, unless a project moved it
    if not findings:
        return []
    # the rule id is what .pr-analyzer.toml's `disable` takes, for a rule a project finds noisy
    counts = Counter((s.title, s.rule_id) for s in findings).most_common()
    kind = "Low-severity findings" if min_severity == "MEDIUM" else f"Findings below {min_severity}"
    lines = ["", f"**{kind} ({len(findings)}), counted but not commented inline:**"]
    lines += [
        f"- {count} × {title}" + (f" (`{rule}`)" if rule else "")
        for (title, rule), count in counts[:MAX_LISTED_IN_REVIEW]
    ]
    if len(counts) > MAX_LISTED_IN_REVIEW:
        rest = sum(count for _, count in counts[MAX_LISTED_IN_REVIEW:])
        lines.append(f"- … and {rest} more")
    return lines


class AnalysisPipeline:
    def __init__(self, skip_llm: bool = False, post_review: bool = False):
        self.github   = GithubClient()
        self.static   = StaticAnalyzer()
        self.security = SecurityScanner()
        self.llm      = LLMEngine()
        self.scorer   = Scorer()
        self.skip_llm = skip_llm
        self.post_review = post_review
        # settings from --config; None reads .pr-analyzer.toml from each PR's base branch
        self.project_config: ProjectConfig | None = None

    def close(self) -> None:
        self.github.close()

    def run(self, url: str, skip_llm: bool | None = None) -> AnalysisResult:
        # the constructor's flag applies unless a caller overrides it for this run; run() used
        # to ignore it, so --skip-llm had no effect
        skip_llm = self.skip_llm if skip_llm is None else skip_llm
        start = time.perf_counter()
        warnings: list[str] = []

        log.info("Fetching PR")
        pr = self.github.fetch_pr(url)
        log.info("PR fetched successfully", repo=pr.repo, number=pr.number, files=len(pr.files))

        config = self._project_config_for(pr)
        # files matching ignore_paths aren't analysed at all, so they never reach the LLM either
        skipped = [f for f in pr.files if config.ignores(f.filename)]
        files = [f for f in pr.files if not config.ignores(f.filename)]
        if skipped:
            warnings.append(skipped_files_warning(skipped))

        log.info("Static analysis starting..")
        static_analysis = self.static.analyze(files, config.disable, config.thresholds)

        log.info("Security analysis starting..")
        security_analysis = self.security.scan(files, config.disable)
        warnings.extend(self.security.warnings)

        # findings on lines marked `pr-analyzer: ignore` are dropped and named in a warning
        existing_findings, ignored = split_ignored(static_analysis + security_analysis, files)

        llm_suggestions: list[Suggestion] = []
        llm_provider_used = None
        if skip_llm or not config.enabled(LLM_RULE):
            log.info("skipping LLM analysis")
        else:
            log.info("running LLM analysis")
            reviewed = replace(pr, files=files) if skipped else pr
            try:
                llm_suggestions = self.llm.analyze(reviewed, existing_findings)
                llm_provider_used = settings.llm_provider
            except Exception as e:  # the LLM review is optional; keep the deterministic results
                log.warning("llm_failed", error=str(e))
                warnings.append(f"LLM review skipped: {e}")
            log.info("LLM analysis done", count=len(llm_suggestions))

        llm_suggestions, ignored_llm = split_ignored(llm_suggestions, files)
        ignored += ignored_llm
        if ignored:
            warnings.append(ignored_warning(ignored))

        all_suggestions = existing_findings + llm_suggestions

        log.info("Scoring: ")
        score = self.scorer.score(all_suggestions, block_on=config.block_on)

        result = AnalysisResult(
            pr=pr,
            suggestions=all_suggestions,
            score=score,
            duration=round(time.perf_counter() - start, 2),
            llm_provider_used=llm_provider_used,
            warnings=warnings,
        )

        if self.post_review:
            self._post_github_review(result, config.inline_min_severity)

        log.info("pipeline complete",
                 score=score.overall_score,
                 grade=score.grade,
                 passed=score.passed,
                 duration=result.duration
        )
        return result

    def _project_config_for(self, pr: PRSummary) -> ProjectConfig:
        # --config wins; otherwise the copy on the PR's base branch, never the PR's own, so a PR
        # can't loosen its own checks. No file there means the defaults.
        if self.project_config is not None:
            return self.project_config
        raw = self.github.fetch_file(pr.repo, CONFIG_FILE, pr.base_branch)
        if raw is None:
            return ProjectConfig()
        log.info("project config loaded", file=CONFIG_FILE, ref=pr.base_branch)
        return parse_project_config(raw, source=f"{CONFIG_FILE} on {pr.base_branch}")

    def _post_github_review(self, result: AnalysisResult, min_severity: str = "MEDIUM") -> None:
        comments = self._review_comments(result, min_severity)
        body = self._build_review_body(result, comments, min_severity)

        log.info("posting github review", comments=len(comments))
        try:
            self.github.post_review(result.pr, body, comments)
        except PRAnalyzerError as e:
            # the analysis itself succeeded; report the posting problem rather than losing it
            log.warning("review_post_failed", error=str(e))
            result.warnings.append(f"Could not post the GitHub review: {e}")

    def _review_comments(
        self, result: AnalysisResult, min_severity: str = "MEDIUM"
    ) -> list[tuple[Suggestion, int]]:
        # GitHub accepts inline comments only on lines that appear in the diff
        diff_lines = {f.filename: commentable_lines(f.patch) for f in result.pr.files if f.patch}
        comments = []
        for s in result.suggestions:
            if not gets_inline_comment(s, min_severity):
                continue
            if s.line is not None and s.line in diff_lines.get(s.filename, set()):
                comments.append((s, s.line))
        return comments

    def _build_review_body(
        self,
        result: AnalysisResult,
        comments: list[tuple[Suggestion, int]],
        min_severity: str = "MEDIUM",
    ) -> str:
        score = result.score
        status = "✅ PASSED" if score.passed else "❌ BLOCKED"

        lines = [
            "## PR Analysis Report",
            "",
            "| Score | Grade | Status | Critical | High |",
            "|-------|-------|--------|----------|------|",
            f"| {score.overall_score:.1f} | {score.grade} | {status} | {score.critical_count} | {score.high_count} |",
            "",
            f"**Summary:** {score.summary}",
        ]

        inline = {id(s) for s, _ in comments}
        commentable = [s for s in result.suggestions if gets_inline_comment(s, min_severity)]
        lines += findings_off_the_diff([s for s in commentable if id(s) not in inline])
        lines += low_severity_counts(
            [s for s in result.suggestions if not gets_inline_comment(s, min_severity)], min_severity
        )

        if result.warnings:
            lines += ["", "**Notes:**"] + [f"- {w}" for w in result.warnings]

        lines += ["", "---", f"*Generated by pr-analyzer — {result.duration:.2f}s*"]
        return "\n".join(lines)
