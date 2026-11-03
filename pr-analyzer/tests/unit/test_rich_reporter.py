import json
import pytest
from io import StringIO
from unittest.mock import patch
from rich.console import Console
from pr_analyzer.reporting import rich_reporter
from pr_analyzer.reporting.rich_reporter import RichReporter
from pr_analyzer.models import AnalysisResult, PRSummary, QualityScore, Suggestion

def make_result(suggestions=None, title="Test PR", warnings=None):
    pr = PRSummary(
        number=1,
        title=title,
        author="neil",
        repo="neil/repo",
        base_branch="main",
        head_branch="feature",
        total_additions=10,
        total_deletions=2,
        is_draft=False,
        files=[]
    )
    score = QualityScore(
        overall_score=95.0,
        grade="A",
        passed=True,
        summary="Clean pass",
        delta=None,
        dimension_scores={
            "security": 100.0,
            "complexity": 100.0,
            "style": 100.0,
            "documentation": 100.0,
            "testing": 100.0,
        },
        critical_count=0,
        high_count=0,
    )
    return AnalysisResult(
        pr=pr,
        suggestions=suggestions or [],
        score=score,
        duration=1.23,
        warnings=warnings or [],
    )

def capture_print(call) -> str:
    captured = StringIO()
    with patch("builtins.print", side_effect=lambda x: captured.write(x)):
        call()
    return captured.getvalue()

@pytest.fixture
def rich_output(monkeypatch):
    # point the reporter's console at a buffer so the rendered text can be checked
    buffer = StringIO()
    monkeypatch.setattr(rich_reporter, "console", Console(file=buffer, width=200, color_system=None))
    return buffer

reporter = RichReporter()

# ── JSON output is valid JSON ─────────────────────────────────────────────────
def test_json_output_is_valid():
    result = make_result(warnings=["Semgrep scan skipped: semgrep is not installed"])
    parsed = json.loads(capture_print(lambda: reporter.print_json_report(result)))
    assert "score" in parsed
    assert "suggestions" in parsed
    assert "pr" in parsed
    assert parsed["warnings"] == ["Semgrep scan skipped: semgrep is not installed"]

def test_json_names_each_findings_rule():
    # the rule id is what a .pr-analyzer.toml `disable` list takes
    s = Suggestion(filename="a.py", severity="LOW", category="style", title="Possible magic number",
                   description="d", line=3, rule_id="magic-number")
    parsed = json.loads(capture_print(lambda: reporter.print_json_report(make_result(suggestions=[s]))))
    assert parsed["suggestions"][0]["rule"] == "magic-number"

# ── Rich output does not crash on empty suggestions ───────────────────────────
def test_rich_report_empty_suggestions_no_exception():
    result = make_result(suggestions=[])
    try:
        reporter.print_rich_report(result)
    except Exception as e:
        pytest.fail(f"print_rich_report raised an exception: {e}")

# ── Rich output does not crash with CRITICAL suggestion ───────────────────────
def test_rich_report_critical_no_exception():
    s = Suggestion(
        filename="auth.py",
        severity="CRITICAL",
        category="security",
        title="Hardcoded password",
        description="Password is hardcoded.",
        line=10,
        source="static"
    )
    result = make_result(suggestions=[s])
    try:
        reporter.print_rich_report(result)
    except Exception as e:
        pytest.fail(f"print_rich_report raised an exception: {e}")

# ── suggestions sorted correctly ──────────────────────────────────────────────
def test_suggestions_sorted_by_severity():
    suggestions = [
        Suggestion(filename="a.py", severity="LOW",      category="style",    title="", description="", source="static"),
        Suggestion(filename="b.py", severity="CRITICAL",  category="security", title="", description="", source="static"),
        Suggestion(filename="c.py", severity="MEDIUM",    category="style",    title="", description="", source="static"),
        Suggestion(filename="d.py", severity="HIGH",      category="security", title="", description="", source="static"),
    ]
    sorted_list = reporter._sort_suggestions(suggestions)
    assert [s.severity for s in sorted_list] == ["CRITICAL", "HIGH", "MEDIUM", "LOW"]

# ── markdown output is a string ───────────────────────────────────────────────
def test_markdown_output_no_exception():
    result = make_result()
    try:
        reporter.print_markdown_report(result)
    except Exception as e:
        pytest.fail(f"print_markdown_report raised an exception: {e}")

# ── untrusted text ────────────────────────────────────────────────────────────
def test_markup_in_pr_text_is_shown_literally(rich_output):
    # "[docs]" used to vanish and "[/]" raised MarkupError, killing the report
    s = Suggestion(filename="app/[slug]/page.tsx", severity="HIGH", category="security",
                   title="Strip [/] from input", description="Regex [/\\] is unsafe", line=4)
    reporter.print_rich_report(make_result(suggestions=[s], title="[docs] Update README"))

    text = rich_output.getvalue()
    assert "[docs] Update README" in text
    assert "app/[slug]/page.tsx:4" in text
    assert "Strip [/] from input" in text

def test_warnings_and_duration_in_rich_report(rich_output):
    reporter.print_rich_report(make_result(warnings=["LLM review skipped: Ollama is down"]))
    text = rich_output.getvalue()
    assert "LLM review skipped: Ollama is down" in text
    assert "duration 1.23s" in text

def test_markdown_escapes_tables_and_omits_missing_lines():
    s = Suggestion(filename="a.py", severity="HIGH", category="security",
                   title="a | b", description="multi\nline", line=None)
    markdown = capture_print(lambda: reporter.print_markdown_report(make_result(suggestions=[s])))
    assert "a \\| b" in markdown
    assert "line None" not in markdown
    assert "> multi line" in markdown
