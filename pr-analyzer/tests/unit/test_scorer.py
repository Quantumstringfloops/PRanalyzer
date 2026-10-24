import pytest
from pr_analyzer.analyzer.scorer import Scorer
from pr_analyzer.models import Suggestion, PRSummary

def make_suggestion(severity="HIGH", category="security", confidence=1.0):
    return Suggestion(
        filename="auth.py",
        severity=severity,
        category=category,
        title="Test",
        description="Test",
        line=1,
        source="static",
        confidence=confidence
    )

def make_pr():
    return PRSummary(
        number=1, title="Test PR", author="neil",
        repo="neil/repo", base_branch="main", head_branch="fix",
        total_additions=10, total_deletions=2,
        is_draft=False, files=[]
    )

scorer = Scorer()

def test_clean_code_scores_above_90():
    result = scorer.score([], make_pr())
    assert result.overall_score >= 90
    assert result.grade in ("A+", "A", "A-")

def test_single_critical_scores_below_90():
    result = scorer.score([make_suggestion(severity="CRITICAL")], make_pr())
    assert result.overall_score < 90
    assert result.passed is False

def test_critical_sets_passed_false():
    result = scorer.score([make_suggestion(severity="CRITICAL")], make_pr())
    assert result.passed is False

def test_low_finding_passes():
    result = scorer.score([make_suggestion(severity="LOW")], make_pr())
    assert result.passed is True

def test_known_formula_output():
    # one HIGH security finding, confidence 1.0
    # security dimension: 100 - (20 × 1.0) = 80
    # weighted: 80×0.35 + 100×0.25 + 100×0.15 + 100×0.15 + 100×0.10 = 28+25+15+15+10 = 93
    result = scorer.score([make_suggestion(severity="HIGH", category="security")], make_pr())
    assert abs(result.overall_score - 93.0) < 0.1

def test_confidence_reduces_penalty():
    full = scorer.score([make_suggestion(severity="HIGH", confidence=1.0)], make_pr())
    half = scorer.score([make_suggestion(severity="HIGH", confidence=0.5)], make_pr())
    assert half.overall_score > full.overall_score