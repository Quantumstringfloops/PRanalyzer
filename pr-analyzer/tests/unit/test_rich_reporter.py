import json
import pytest
from io import StringIO
from unittest.mock import patch
from pr_analyzer.reporting.rich_reporter import RichReporter
from pr_analyzer.models import PRSummary, Suggestion, FileChange
from pr_analyzer.pipeline import AnalysisResult
from pr_analyzer.analyzer.scorer import QualityScore

def make_result(suggestions=None):
    pr = PRSummary(
        number=1,
        title="Test PR",
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
    )

reporter = RichReporter()

# ── JSON output is valid JSON ─────────────────────────────────────────────────
def test_json_output_is_valid():
    result = make_result()
    captured = StringIO()
    with patch("builtins.print", side_effect=lambda x: captured.write(x)):
        reporter.print_json_report(result)
    parsed = json.loads(captured.getvalue())
    assert "score" in parsed
    assert "suggestions" in parsed
    assert "pr" in parsed

# ── Rich output does not crash on empty suggestions ───────────────────────────
def test_rich_report_empty_suggestions_no_exception():
    result = make_result(suggestions=[])
    try:
        reporter.print_rich_report(result, duration=1.0)
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
        reporter.print_rich_report(result, duration=1.0)
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