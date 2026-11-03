import json
from datetime import datetime
from types import SimpleNamespace

import pytest
import structlog
from rich.text import Text
from typer.testing import CliRunner

from pr_analyzer import cli
from pr_analyzer.exceptions import InvalidPRURLError, PrivateRepoError, RateLimitError
from pr_analyzer.models import AnalysisResult, PRSummary, QualityScore

URL = "https://github.com/neil/repo/pull/1"
runner = CliRunner()

def make_result(passed=True):
    pr = PRSummary(number=1, title="Test PR", author="neil", repo="neil/repo", base_branch="main",
                   head_branch="feature", total_additions=1, total_deletions=0, is_draft=False)
    score = QualityScore(
        overall_score=95.0 if passed else 60.0,
        grade="A" if passed else "F",
        passed=passed,
        summary="summary",
        delta=None,
        dimension_scores={"security": 100.0, "complexity": 100.0, "style": 100.0,
                          "documentation": 100.0, "testing": 100.0},
        critical_count=0 if passed else 1,
        high_count=0,
    )
    return AnalysisResult(pr=pr, suggestions=[], score=score, duration=0.5)

@pytest.fixture
def pipeline(monkeypatch):
    """Replace the real pipeline; `state` controls what run() does and records what it saw."""
    state = SimpleNamespace(result=make_result(), error=None, instances=[])

    class FakePipeline:
        def __init__(self, skip_llm=False, post_review=False, use_cache=True):
            self.skip_llm, self.post_review, self.closed = skip_llm, post_review, False
            self.use_cache = use_cache
            state.instances.append(self)

        def run(self, url):
            structlog.get_logger("fake").info("fake_pipeline_ran", url=url)
            if state.error is not None:
                raise state.error
            return state.result

        def close(self):
            self.closed = True

    monkeypatch.setattr(cli, "AnalysisPipeline", FakePipeline)
    return state

def invoke(*args):
    return runner.invoke(cli.app, ["analyze", URL, *args])

# ── flags ─────────────────────────────────────────────────────────────────────
def test_flags_reach_the_pipeline(pipeline):
    # Typer 0.12 on Click >= 8.2 delivered these as the string 'False' or None
    result = invoke("--skip-llm", "--post-review")

    assert result.exit_code == 0
    [instance] = pipeline.instances
    assert (instance.skip_llm, instance.post_review, instance.closed) == (True, True, True)

def test_flag_defaults(pipeline):
    invoke()
    [instance] = pipeline.instances
    assert (instance.skip_llm, instance.post_review, instance.use_cache) == (False, False, True)

def test_no_cache_flag_reaches_the_pipeline(pipeline):
    assert invoke("--no-cache").exit_code == 0
    [instance] = pipeline.instances
    assert instance.use_cache is False

# ── exit codes ────────────────────────────────────────────────────────────────
def test_blocked_pr_exits_1(pipeline):
    pipeline.result = make_result(passed=False)
    assert invoke().exit_code == 1

def test_fail_on_block_flag_keeps_blocking(pipeline):
    # under Click 8.1 this exact flag (used by pr-analysis.yml) used to switch blocking off
    pipeline.result = make_result(passed=False)
    assert invoke("--fail-on-block").exit_code == 1

def test_no_fail_on_block_reports_without_failing(pipeline):
    pipeline.result = make_result(passed=False)
    assert invoke("--no-fail-on-block").exit_code == 0

def test_invalid_url_exits_2(pipeline):
    pipeline.error = InvalidPRURLError("Domain must be github.com")
    result = invoke()
    assert result.exit_code == 2
    assert "Invalid URL: Domain must be github.com" in result.stderr

def test_rate_limit_says_when_to_retry(pipeline):
    pipeline.error = RateLimitError(datetime(2026, 1, 1, 12, 30, 0))
    result = invoke()
    assert result.exit_code == 2
    assert "Try again after 12:30:00" in result.stderr

def test_access_denied_explains_token_scopes(pipeline):
    pipeline.error = PrivateRepoError("Your token is not allowed to access neil/repo")
    assert "fine-grained" in invoke().stderr

def test_unexpected_error_exits_2_not_1(pipeline):
    # crashes used to exit 1, which CI could not tell apart from a blocked PR
    pipeline.error = RuntimeError("boom")
    result = invoke()
    assert result.exit_code == 2
    assert "Unexpected error" in result.stderr
    assert pipeline.instances[0].closed is True

# ── output formats ────────────────────────────────────────────────────────────
def test_json_stdout_stays_parseable_with_verbose_logs(pipeline):
    # logs used to be printed to stdout, in the middle of the JSON
    result = invoke("--output", "json", "--verbose")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["score"]["grade"] == "A"
    assert "fake_pipeline_ran" in result.stderr

def test_markdown_output(pipeline):
    result = invoke("-o", "markdown")
    assert result.stdout.startswith("# PR Analysis Report")

def test_unknown_output_format_rejected(pipeline):
    result = invoke("--output", "xml")
    assert result.exit_code == 2
    assert pipeline.instances == []

# ── help and version ──────────────────────────────────────────────────────────
def test_help_works():
    # --help used to crash with TypeError under the locked Click
    assert runner.invoke(cli.app, ["--help"]).exit_code == 0
    # wide enough that the option names aren't cut short
    result = runner.invoke(cli.app, ["analyze", "--help"], env={"COLUMNS": "200", "TERMINAL_WIDTH": "200"})
    assert result.exit_code == 0
    # Typer forces colour on GitHub Actions, and the escape codes split the option names up
    assert "--no-fail-on-block" in Text.from_ansi(result.stdout).plain

def test_version():
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("pr-analyzer ")
