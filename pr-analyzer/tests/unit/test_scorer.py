from pr_analyzer.analyzer.scorer import Scorer
from pr_analyzer.models import Suggestion

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

scorer = Scorer()

# these tests used to pass a PRSummary as the second argument, which is the `base` score,
# so every one of them failed with a TypeError

def test_clean_code_scores_above_90():
    result = scorer.score([])
    assert result.overall_score >= 90
    assert result.grade in ("A+", "A", "A-")

def test_single_critical_scores_below_90():
    result = scorer.score([make_suggestion(severity="CRITICAL")])
    assert result.overall_score < 90
    assert result.passed is False

def test_critical_sets_passed_false():
    result = scorer.score([make_suggestion(severity="CRITICAL")])
    assert result.passed is False

def test_low_finding_passes():
    result = scorer.score([make_suggestion(severity="LOW")])
    assert result.passed is True

def test_known_formula_output():
    # one HIGH security finding, confidence 1.0
    # security dimension: 100 - (20 × 1.0) = 80
    # weighted: 80×0.35 + 100×0.25 + 100×0.15 + 100×0.15 + 100×0.10 = 28+25+15+15+10 = 93
    result = scorer.score([make_suggestion(severity="HIGH", category="security")])
    assert abs(result.overall_score - 93.0) < 0.1

def test_confidence_reduces_penalty():
    full = scorer.score([make_suggestion(severity="HIGH", confidence=1.0)])
    half = scorer.score([make_suggestion(severity="HIGH", confidence=0.5)])
    assert half.overall_score > full.overall_score

def test_lowercase_severity_counts():
    result = scorer.score([make_suggestion(severity="high")])
    assert result.passed is False
    assert result.dimension_scores["security"] == 80.0

def test_unknown_severity_is_ignored_instead_of_crashing():
    # "MEDUIM" used to raise KeyError and abort the run
    result = scorer.score([make_suggestion(severity="MEDUIM")])
    assert result.overall_score == 100.0

def test_unknown_category_is_scored_as_style():
    result = scorer.score([make_suggestion(severity="MEDIUM", category="performance")])
    assert result.dimension_scores["style"] == 92.0

def test_confidence_is_clamped():
    too_high = scorer.score([make_suggestion(severity="HIGH", confidence=5.0)])
    negative = scorer.score([make_suggestion(severity="HIGH", confidence=-1.0)])
    assert too_high.dimension_scores["security"] == 80.0
    assert negative.dimension_scores["security"] == 100.0

def test_dimension_never_goes_below_zero():
    result = scorer.score([make_suggestion(severity="CRITICAL")] * 5)
    assert result.dimension_scores["security"] == 0.0

def test_grade_matches_the_displayed_score():
    # unrounded 96.96 used to be shown as "97.0" but graded A instead of A+
    result = scorer.score([make_suggestion(severity="MEDIUM", confidence=0.543)] * 2)
    assert result.overall_score == 97.0
    assert result.grade == "A+"

def test_delta_against_base():
    assert scorer.score([], base=90.0).delta == 10.0
    assert scorer.score([]).delta is None

# ── blocking severities from .pr-analyzer.toml ────────────────────────────────
def test_blocking_severities_are_configurable():
    medium = [make_suggestion(severity="MEDIUM")]
    high = [make_suggestion(severity="HIGH")]

    assert scorer.score(medium, block_on={"CRITICAL", "HIGH", "MEDIUM"}).passed is False
    assert scorer.score(high, block_on={"CRITICAL"}).passed is True
    assert scorer.score(high, block_on=set()).passed is True

def test_blocked_summary_never_says_clean_pass():
    result = scorer.score([make_suggestion(severity="MEDIUM")], block_on={"MEDIUM"})
    assert result.overall_score >= 90
    assert "Clean pass" not in result.summary
    assert "blocking" in result.summary
