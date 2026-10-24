import pytest
from unittest.mock import MagicMock, patch
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
        ]
    )

def make_insecure_pr():
    return make_pr(patch_text='@@ -1,1 +1,1 @@\n+password = "secret123"')

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

# ── test: skip_llm bypasses LLM engine ───────────────────────────────────────
def test_skip_llm_bypasses_engine():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze") as mock_llm:
            pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=True)
            mock_llm.assert_not_called()

# ── test: duration is measured ────────────────────────────────────────────────
def test_duration_is_measured():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.llm, "analyze", return_value=[]):
            result = pipeline.run("https://github.com/neil/repo/pull/1", skip_llm=False)

    assert result.analysis_duration_seconds > 0

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