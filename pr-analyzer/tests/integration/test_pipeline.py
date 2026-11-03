import pytest
from unittest.mock import patch
from pr_analyzer import pipeline as pipeline_module
from pr_analyzer.exceptions import ConfigError, InvalidTokenError, LLMError
from pr_analyzer.pipeline import AnalysisPipeline
from pr_analyzer.models import PRSummary, FileChange, Suggestion
from pr_analyzer.project_config import ProjectConfig

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
    return make_pr(patch_text='@@ -1,1 +1,1 @@\n+password = "secret123"')  # pr-analyzer: ignore

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

# ── test: ignore markers ──────────────────────────────────────────────────────
def test_marked_lines_are_ignored_and_reported():
    marked = 'password = "secret123"  # pr-analyzer: ignore'
    pr = make_pr(f"@@ -1,1 +1,2 @@\n x = 1\n+{marked}")
    pr.files[0].raw_content = f"x = 1\n{marked}"
    pipeline = AnalysisPipeline(skip_llm=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=pr):
        result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert result.suggestions == []
    assert result.passed is True
    assert "Ignored 1 finding(s) on lines marked 'pr-analyzer: ignore': auth.py:2" in result.warnings

def test_llm_findings_on_marked_lines_are_ignored():
    pr = make_pr("@@ -1,1 +1,1 @@\n+x = 1  # pr-analyzer: ignore")
    pr.files[0].raw_content = "x = 1  # pr-analyzer: ignore"
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=pr):
        with patch.object(pipeline.llm, "analyze", return_value=[make_suggestion(1, "MEDIUM")]):
            result = pipeline.run("https://github.com/neil/repo/pull/1")

    assert result.suggestions == []
    assert any("auth.py:1" in w for w in result.warnings)

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

def test_low_finding_counts_name_their_rule():
    # so a project can see which id to put in .pr-analyzer.toml's `disable`
    pipeline = AnalysisPipeline(skip_llm=True, post_review=True)
    patch_text = "@@ -0,0 +1,2 @@\n+wait(30)\n+wait(45)"

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr(patch_text)):
        with patch.object(pipeline.github, "post_review") as mock_post:
            pipeline.run("https://github.com/neil/repo/pull/1")

    _, body, _ = mock_post.call_args[0]
    assert "- 2 × Possible magic number (`magic-number`)" in body

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

# ── project settings (.pr-analyzer.toml) ──────────────────────────────────────
URL = "https://github.com/neil/repo/pull/1"

def run_with_config(toml, pr=None):
    """Run with `toml` as the base branch's .pr-analyzer.toml; return the result and the fetch mock."""
    pipeline = AnalysisPipeline(skip_llm=True)
    with patch.object(pipeline.github, "fetch_pr", return_value=pr or make_pr()):
        with patch.object(pipeline.github, "fetch_file", return_value=toml.encode()) as fetch:
            result = pipeline.run(URL)
    return result, fetch

def test_config_comes_from_the_base_branch():
    # never from the PR's own head, so a PR can't loosen its own checks
    _, fetch = run_with_config("")
    fetch.assert_called_once_with("neil/repo", ".pr-analyzer.toml", "main")

def test_disabled_rules_are_not_reported():
    result, _ = run_with_config('disable = ["hardcoded-password"]', make_insecure_pr())
    assert result.suggestions == []
    assert result.passed is True

def test_thresholds_reach_the_checks():
    pr = make_pr("@@ -1,1 +1,1 @@\n+total = compute_total()")
    result, _ = run_with_config("[thresholds]\nmax_line_length = 10", pr)
    assert [s.title for s in result.suggestions] == ["Line exceeds 10 characters"]

def test_block_on_decides_what_blocks():
    pr = make_pr("@@ -1,1 +1,1 @@\n+eval(data)")  # pr-analyzer: ignore
    result, _ = run_with_config('block_on = ["CRITICAL"]', pr)
    assert [s.severity for s in result.suggestions] == ["HIGH"]
    assert result.passed is True

def test_ignored_paths_are_not_analysed_or_sent_to_the_llm():
    pr = make_insecure_pr()
    pr.files.append(FileChange(filename="docs/example.py", status="added", additions=1, deletions=0,
                               patch="@@ -0,0 +1,1 @@\n+print(token)", raw_content="print(token)"))
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=pr):
        with patch.object(pipeline.github, "fetch_file", return_value=b'ignore_paths = ["docs/"]'):
            with patch.object(pipeline.llm, "analyze", return_value=[]) as mock_llm:
                result = pipeline.run(URL)

    reviewed = mock_llm.call_args[0][0]
    assert [f.filename for f in reviewed.files] == ["auth.py"]
    assert [s.filename for s in result.suggestions] == ["auth.py"]
    assert "Skipped 1 file(s) matching ignore_paths in .pr-analyzer.toml: docs/example.py" in result.warnings
    assert len(result.pr.files) == 2      # the report still describes the whole PR

def test_llm_can_be_disabled_by_config():
    pipeline = AnalysisPipeline()

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.github, "fetch_file", return_value=b'disable = ["llm"]'):
            with patch.object(pipeline.llm, "analyze") as mock_llm:
                result = pipeline.run(URL)

    mock_llm.assert_not_called()
    assert result.llm_provider_used is None

def test_inline_min_severity_moves_the_comment_threshold():
    pipeline = AnalysisPipeline(skip_llm=True, post_review=True)
    findings = [make_suggestion(1, "MEDIUM", title="Risky"), make_suggestion(1, "HIGH", title="Riskier")]

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.github, "fetch_file", return_value=b'inline_min_severity = "HIGH"'):
            with patch.object(pipeline.static, "analyze", return_value=findings):
                with patch.object(pipeline.security, "scan", return_value=[]):
                    with patch.object(pipeline.github, "post_review") as mock_post:
                        pipeline.run(URL)

    _, body, comments = mock_post.call_args[0]
    assert [s.title for s, _ in comments] == ["Riskier"]
    assert "Findings below HIGH (1), counted but not commented inline" in body
    assert "- 1 × Risky" in body

def test_invalid_base_branch_config_stops_the_run():
    pipeline = AnalysisPipeline(skip_llm=True)

    with patch.object(pipeline.github, "fetch_pr", return_value=make_pr()):
        with patch.object(pipeline.github, "fetch_file", return_value=b'disable = ["magic-numbers"]'):
            with pytest.raises(ConfigError, match=r"\.pr-analyzer\.toml on main is invalid"):
                pipeline.run(URL)

def test_explicit_config_replaces_the_base_branch_file():
    pipeline = AnalysisPipeline(skip_llm=True)
    pipeline.project_config = ProjectConfig(disable=frozenset({"hardcoded-password"}))

    with patch.object(pipeline.github, "fetch_pr", return_value=make_insecure_pr()):
        with patch.object(pipeline.github, "fetch_file") as fetch:
            result = pipeline.run(URL)

    fetch.assert_not_called()
    assert result.suggestions == []
