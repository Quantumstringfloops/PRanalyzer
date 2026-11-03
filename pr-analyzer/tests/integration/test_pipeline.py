import pytest
from unittest.mock import patch
from pr_analyzer import pipeline as pipeline_module
from pr_analyzer.exceptions import InvalidTokenError, LLMError
from pr_analyzer.pipeline import AnalysisPipeline
from pr_analyzer.models import PRSummary, FileChange, Suggestion

# ── helpers ───────────────────────────────────────────────────────────────────

def make_pr(patch_text="@@ -1,1 +1,1 @@\n+x = 1"):
    return PRSummary(
        number=1,
        title="Test PR",
        author="neil",
        repo="neil/repo",
        base_branch="main",
        head_branch="feature",
        total_additions=5,
        total_deletions=1,
        is_draft=False,
        files=[
            FileChange(
                filename="auth.py",
                status="modified",
                additions=5,
                deletions=1,
                patch=patch_text,
                raw_content="x = 1",
            )
        ],
        head_sha="abc123",
    )

def make_insecure_pr():
    return make_pr(patch_text='@@ -1,1 +1,1 @@\n+password = "secret123"')

def make_suggestion(line, severity="LOW", title="TODO"):
    return Suggestion(filename="auth.py", severity=severity, category="style",
                      title=title, description="d", line=line, source="static")

# ── test: clean PR passes ─────────────────────────────────────────────────────
def test_clean_pr_passes():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze", return_value=[]):
            result = pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=False)

    assert result.passed is True
    assert result.score.grade in ("A+", "A", "A-")

# ── test: insecure PR is blocked ──────────────────────────────────────────────
def test_insecure_pr_blocked():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_insecure_pr()):
        with patch.object(pipeline.llm, "analyze", return_value=[]):
            result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert result.passed is False
    assert any(s.severity in ("CRITICAL", "HIGH") for s in result.suggestions)
    # one hardcoded password is reported once, on its real line
    assert [(s.title, s.line) for s in result.suggestions] == [("Hardcoded password", 1)]

# ── test: skip_llm bypasses LLM engine ───────────────────────────────────────
def test_skip_llm_bypasses_engine():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze") as mock_llm:
            pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=True)
            mock_llm.assert_not_called()

def test_skip_llm_from_constructor_is_honoured():
    # the CLI passes --skip-llm to the constructor, which run() used to ignore
    pipeline = AnalysisPipeline(skip_llm=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze") as mock_llm:
            result = pipeline.run("https://github.com/neil/repo/pull/1")

    mock_llm.assert_not_called()
    assert result.llm_provider_used is None

# ── test: duration is measured ────────────────────────────────────────────────
def test_duration_is_measured(monkeypatch):
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(pipeline_module.time, "perf_counter", lambda: next(ticks))
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze", return_value=[]):
            result = pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=False)

    assert result.duration == 2.5

# ── test: all suggestions merged ─────────────────────────────────────────────
def test_all_suggestions_merged():
    pipeline = AnalysisPipeline()

    static_finding = Suggestion(
        filename="auth.py", severity="LOW", category="style",
        title="TODO", description="todo found", source="static"
    )
    llm_finding = Suggestion(
        filename="auth.py", severity="MEDIUM", category="complexity",
        title="Complex function", description="too complex", source="llm"
    )

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.static, "analyze", return_value=[static_finding]):
            with patch.object(pipeline.security, "scan", return_value=[]):
                with patch.object(pipeline.llm, "analyze", return_value=[llm_finding]):
                    result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert len(result.suggestions) == 2

# ── test: LLM receives existing findings ─────────────────────────────────────
def test_llm_receives_existing_findings():
    pipeline = AnalysisPipeline()

    static_finding = Suggestion(
        filename="auth.py", severity="HIGH", category="security",
        title="Issue", description="desc", source="static"
    )

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.static, "analyze", return_value=[static_finding]):
            with patch.object(pipeline.security, "scan", return_value=[]):
                with patch.object(pipeline.llm, "analyze") as mock_llm:
                    mock_llm.return_value = []
                    pipeline.run("https://github.com/neil/repo/pull/1")
                    # check existing_findings was passed to LLM
                    call_args = mock_llm.call_args
                    existing = call_args[0][1]  # second positional argument
                    assert static_finding in existing

# ── test: optional stages degrade instead of failing ─────────────────────────
def test_llm_failure_keeps_other_findings():
    # an LLM error used to abort the run and throw away the static and security results
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_insecure_pr()):
        with patch.object(pipeline.llm, "analyze", side_effect=LLMError("Could not reach Ollama")):
            result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert [s.title for s in result.suggestions] == ["Hardcoded password"]
    assert "LLM review skipped: Could not reach Ollama" in result.warnings
    assert result.llm_provider_used is None

def test_security_tool_warnings_reach_the_result():
    pipeline = AnalysisPipeline(skip_llm=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        result = pipeline.run("https://github.com/neil/repo/pull/1")

    # the test suite stubs the tools out, as if they weren't installed
    assert any("Semgrep scan skipped" in w for w in result.warnings)

# ── test: posting a review ────────────────────────────────────────────────────
def test_post_review_comments_only_on_diff_lines():
    # --post-review used to be ignored entirely
    pipeline = AnalysisPipeline(skip_llm=True, post_review=True)
    in_diff, outside_diff, no_line = (make_suggestion(line, "MEDIUM") for line in (1, 50, None))

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.static, "analyze", return_value=[in_diff, outside_diff, no_line]):
            with patch.object(pipeline.security, "scan", return_value=[]):
                with patch.object(pipeline.github, "post_review") as mock_post:
                    pipeline.run("https://github.com/neil/repo/pull/1")

    pr, body, comments = mock_post.call_args[0]
    assert comments == [(in_diff, 1)]
    assert "auth.py:50" in body           # findings without an inline comment are listed
    assert "## PR Analysis Report" in body

def test_low_findings_are_counted_not_commented():
    # a comment on every LOW finding used to bury the ones that matter
    pipeline = AnalysisPipeline(skip_llm=True, post_review=True)
    high = make_suggestion(1, "HIGH", title="Risky call")
    findings = [make_suggestion(1), make_suggestion(1), make_suggestion(None, title="Long line"), high]

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.static, "analyze", return_value=findings):
            with patch.object(pipeline.security, "scan", return_value=[]):
                with patch.object(pipeline.github, "post_review") as mock_post:
                    pipeline.run("https://github.com/neil/repo/pull/1")

    pr, body, comments = mock_post.call_args[0]
    assert comments == [(high, 1)]
    assert "Low-severity findings (3)" in body
    assert "- 2 × TODO" in body and "- 1 × Long line" in body
    assert "Findings not attached" not in body   # LOW findings are no longer listed one by one

def test_post_review_failure_becomes_warning():
    pipeline = AnalysisPipeline(skip_llm=True, post_review=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.github, "post_review", side_effect=InvalidTokenError("no token")):
            result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert any("Could not post the GitHub review" in w for w in result.warnings)

def test_no_review_posted_without_flag():
    pipeline = AnalysisPipeline(skip_llm=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.github, "post_review") as mock_post:
            pipeline.run("https://github.com/neil/repo/pull/1")

    mock_post.assert_not_called()

@pytest.mark.parametrize("skip_llm", [True, False])
def test_result_passed_follows_score(skip_llm):
    pipeline = AnalysisPipeline()
    with patch.object(pipeline.github, "fetch_pr", return_value=make_insecure_pr()):
        with patch.object(pipeline.llm, "analyze", return_value=[]):
            result = pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=skip_llm)
    assert result.passed is result.score.passed is False
