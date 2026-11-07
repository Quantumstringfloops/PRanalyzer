"""The decision engine, and above all its fail-safe paths: nothing incomplete, from a fork or
otherwise doubtful may come out as a merge."""
import itertools

import pytest
from conftest import make_file, make_finding, make_outcome, make_pull, make_result

from review_app.decision import (
    Verdict,
    compute_confidence,
    coverage_factor,
    decide,
    size_factor,
)
from review_app.models import ChangedFile
from review_app.policy import Policy

POLICY = Policy()


def test_a_clean_complete_analysis_merges():
    decision = decide(make_pull(), POLICY, make_outcome())

    assert decision.verdict is Verdict.MERGE
    assert decision.complete
    assert decision.confidence == 1.0
    assert "clear this repository's merge thresholds" in decision.reasons[0]
    assert decision.facts["policy"]["mode"] == "recommend"
    assert decision.facts["score"] == 95.0


# ── block ────────────────────────────────────────────────────────────────────────────────
def test_blocking_findings_block():
    result = make_result(score=70, grade="C-", passed=False,
                         suggestions=[make_finding("CRITICAL", title="Hardcoded password")])
    decision = decide(make_pull(), POLICY, make_outcome(result))

    assert decision.verdict is Verdict.BLOCK
    assert "blocking issues (1 critical, 0 high)" in decision.reasons[0]
    assert "Hardcoded password" in decision.reasons[0]


def test_blocking_wins_over_an_incomplete_analysis():
    # a fuller analysis could only find more
    result = make_result(passed=False, suggestions=[make_finding("HIGH")],
                         warnings=["Semgrep scan skipped: semgrep is not installed"])
    decision = decide(make_pull(), POLICY, make_outcome(result))

    assert decision.verdict is Verdict.BLOCK
    assert not decision.complete
    assert "also incomplete" in decision.reasons[-1]


@pytest.mark.parametrize(("score", "grade"), [(60.0, "F"), (64.9, "F")])
def test_a_score_or_grade_under_the_block_line_blocks(score, grade):
    decision = decide(make_pull(), POLICY, make_outcome(make_result(score=score, grade=grade)))
    assert decision.verdict is Verdict.BLOCK


def test_block_thresholds_come_from_the_policy():
    strict = Policy(block_below_score=85, block_below_grade="B")
    decision = decide(make_pull(), strict, make_outcome(make_result(score=80, grade="B-")))
    assert decision.verdict is Verdict.BLOCK
    assert any("block_below_score (85)" in r for r in decision.reasons)
    assert any("block_below_grade (B)" in r for r in decision.reasons)


# ── an incomplete analysis never merges ──────────────────────────────────────────────────
@pytest.mark.parametrize("warning", [
    "Semgrep scan skipped: semgrep is not installed (pip install 'pr-analyzer-cli[security]')",
    "Semgrep scan failed (exit 2): invalid config",
    "Dependency audit skipped: pip-audit took longer than 300s",
    "LLM review skipped: NVIDIA_API_KEY is not set: create a key at build.nvidia.com",
    "2 of 5 file(s) were not AI-reviewed, or only in part: an LLM request failed (HTTP 503)",
    "1 of 1 LLM reply(ies) couldn't be read as JSON, so the files in them were not AI-reviewed",
    "Skipped 1 file(s) matching ignore_paths in .pr-analyzer.toml: vendor/lib.js",
    ".pr-analyzer.toml on main is invalid, so the default settings were used: bad key",
    "Ignored 1 finding(s) on lines marked 'pr-analyzer: ignore': src/app.py:3",
    "A warning a later pr-analyzer adds, worded in a way this version has never seen",
])
def test_any_warning_makes_the_analysis_incomplete(warning):
    decision = decide(make_pull(), POLICY, make_outcome(make_result(warnings=[warning])))

    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert not decision.complete
    assert any(warning in reason for reason in decision.reasons)


def test_a_review_that_could_not_be_posted_is_not_incomplete():
    result = make_result(warnings=["Could not post the GitHub review: GitHub error 502"])
    decision = decide(make_pull(), POLICY, make_outcome(result))
    assert decision.verdict is Verdict.MERGE


def test_a_file_without_a_diff_was_never_analysed():
    changed = (ChangedFile("src/app.py", "modified", 12, True),
               ChangedFile("assets/logo.png", "added", 0, False))
    decision = decide(make_pull(changed_files=2), POLICY, make_outcome(changed=changed))

    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("came without a diff" in r and "assets/logo.png" in r for r in decision.reasons)


def test_a_pure_rename_has_nothing_to_analyse():
    changed = (ChangedFile("src/app.py", "modified", 12, True),
               ChangedFile("docs/new.md", "renamed", 0, False, previous_filename="docs/old.md"))
    decision = decide(make_pull(changed_files=2), POLICY, make_outcome(changed=changed))
    assert decision.verdict is Verdict.MERGE


def test_files_github_did_not_list_were_not_analysed():
    decision = decide(make_pull(changed_files=3001), POLICY, make_outcome())
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("listed only 1 of the 3001" in r for r in decision.reasons)


def test_files_that_could_not_be_listed_count_as_skipped():
    decision = decide(make_pull(), POLICY, make_outcome(changed=None))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("couldn't be listed" in r for r in decision.reasons)


def test_content_that_could_not_be_fetched_leaves_the_analysis_incomplete():
    result = make_result(files=[make_file(content=None)])
    decision = decide(make_pull(), POLICY, make_outcome(result))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("full content of 1 file(s)" in r for r in decision.reasons)


@pytest.mark.parametrize("tool", ["semgrep", "pip-audit"])
def test_a_missing_scanner_means_no_merge(tool):
    decision = decide(make_pull(), POLICY, make_outcome(missing_tools=(tool,)))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any(f"{tool} isn't installed" in r for r in decision.reasons)


@pytest.mark.parametrize("stage", ["semgrep", "dependency-audit", "llm"])
def test_a_stage_switched_off_by_the_project_means_no_merge(stage):
    decision = decide(make_pull(), POLICY, make_outcome(disabled_stages=(stage,)))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any(f"the {stage} check is switched off" in r for r in decision.reasons)


def test_the_ai_review_may_be_switched_off_when_the_policy_allows_it():
    lenient = Policy(require_llm_review=False)
    outcome = make_outcome(make_result(llm=None), disabled_stages=("llm",))
    assert decide(make_pull(), lenient, outcome).verdict is Verdict.MERGE


def test_new_commits_during_the_analysis_leave_it_incomplete():
    decision = decide(make_pull(), POLICY, make_outcome(head_moved=True))
    assert decision.verdict is Verdict.NEEDS_REVIEW


def test_a_failed_analysis_needs_review():
    decision = decide(make_pull(), POLICY, None, "GitHub error 500: boom")

    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert decision.confidence == 0.0
    assert not decision.complete
    assert decision.reasons[0] == "The analysis didn't finish: GitHub error 500: boom"


# ── the PR itself ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("head_repo_id", [2, None])
def test_a_pr_from_a_fork_never_merges(head_repo_id):
    # None: the fork was deleted, so nothing proves the branch is the repository's own
    decision = decide(make_pull(head_repo_id=head_repo_id), POLICY, make_outcome())
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("fork" in r for r in decision.reasons)


def test_a_draft_needs_review():
    decision = decide(make_pull(is_draft=True), POLICY, make_outcome())
    assert decision.verdict is Verdict.NEEDS_REVIEW


def test_only_allowed_base_branches_merge():
    release = make_pull(base_branch="release/1.x")
    assert decide(release, POLICY, make_outcome()).verdict is Verdict.NEEDS_REVIEW
    allowed = Policy(base_branches=("main", "release/1.x"))
    assert decide(release, allowed, make_outcome()).verdict is Verdict.MERGE


@pytest.mark.parametrize("changed", [
    ChangedFile(".github/workflows/ci.yml", "modified", 4, True),
    ChangedFile("CODEOWNERS", "modified", 1, True),
    ChangedFile("docs/CODEOWNERS", "added", 1, True),
    ChangedFile(".pr-analyzer.toml", "modified", 2, True),
    # moving a workflow out of .github changes .github too
    ChangedFile("ci.yml", "renamed", 0, False, previous_filename=".github/workflows/ci.yml"),
])
def test_paths_that_always_need_a_human(changed):
    result = make_result(files=[make_file(changed.filename)])
    decision = decide(make_pull(), POLICY, make_outcome(result, changed=(changed,)))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("always need a human" in r for r in decision.reasons)


def test_a_missing_ai_review_needs_review_when_the_policy_requires_it():
    outcome = make_outcome(make_result(llm=None))
    decision = decide(make_pull(), POLICY, outcome)
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert "The AI review didn't run, and this repository's policy requires it" in decision.reasons
    assert decide(make_pull(), Policy(require_llm_review=False), outcome).verdict is Verdict.MERGE


@pytest.mark.parametrize(("score", "grade"), [(89.9, "B+"), (85.0, "B")])
def test_a_score_under_the_merge_line_needs_review(score, grade):
    decision = decide(make_pull(), POLICY, make_outcome(make_result(score=score, grade=grade)))
    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert any("merge_min_score (90)" in r for r in decision.reasons)
    assert any("merge_min_grade (A-)" in r for r in decision.reasons)


def test_a_grade_threshold_can_be_stricter_than_the_score():
    strict = Policy(merge_min_grade="A")
    decision = decide(make_pull(), strict, make_outcome(make_result(score=91, grade="A-")))
    assert decision.verdict is Verdict.NEEDS_REVIEW


def test_the_decision_does_not_depend_on_the_mode():
    # the mode only changes what is done about a decision, never the decision
    for outcome in (make_outcome(), make_outcome(make_result(llm=None))):
        recommend = decide(make_pull(), Policy(mode="recommend"), outcome)
        auto = decide(make_pull(), Policy(mode="auto"), outcome)
        assert (recommend.verdict, recommend.reasons) == (auto.verdict, auto.reasons)


@pytest.mark.parametrize(("incomplete", "fork", "draft", "missing_scanner"),
                         [flags for flags in itertools.product([False, True], repeat=4)
                          if any(flags)])
def test_no_combination_of_doubts_merges(incomplete, fork, draft, missing_scanner):
    result = make_result(warnings=["LLM review skipped: timeout"] if incomplete else [])
    outcome = make_outcome(result, missing_tools=("semgrep",) if missing_scanner else ())
    pull = make_pull(head_repo_id=2 if fork else 1, is_draft=draft)
    for policy in (Policy(mode="auto"), Policy(mode="auto", merge_min_score=0,
                                               merge_min_grade="F", merge_min_confidence=0,
                                               block_below_score=0, block_below_grade="F")):
        assert decide(pull, policy, outcome).verdict is Verdict.NEEDS_REVIEW


# ── confidence ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("lines", "factor"), [
    (0, 1.0), (400, 1.0), (1200, 0.75), (2000, 0.5), (10_000, 0.5),
])
def test_size_factor(lines, factor):
    assert size_factor(lines) == pytest.approx(factor)


def test_coverage_counts_deeply_parsed_languages_and_docs():
    assert coverage_factor([make_file("a.py"), make_file("README.md")]) == pytest.approx(1.0)
    assert coverage_factor([make_file("main.go")]) == pytest.approx(0.7)
    assert coverage_factor([make_file("a.ts", 6, 0), make_file("b.go", 6, 0)]) == pytest.approx(0.85)
    assert coverage_factor([make_file("a.go", 0, 0)]) == 1.0   # nothing changed


def test_unsure_ai_findings_lower_the_confidence():
    findings = [make_finding("MEDIUM", "llm", 0.6), make_finding("HIGH", "llm", 0.7),
                make_finding("LOW", "llm", 0.5),      # too minor to count
                make_finding("MEDIUM", "llm", 0.9),   # sure enough
                make_finding("MEDIUM", "static", 0.5)]   # not the AI
    confidence = compute_confidence(make_outcome(make_result(suggestions=findings)))
    assert confidence.ai_certainty == pytest.approx(0.8)
    many = [make_finding("MEDIUM", "llm", 0.5)] * 10
    assert compute_confidence(make_outcome(make_result(suggestions=many))).ai_certainty == 0.6


def test_a_big_change_falls_under_the_confidence_line():
    result = make_result(files=[make_file("src/app.py", additions=1500, deletions=100)])
    decision = decide(make_pull(), POLICY, make_outcome(result))

    assert decision.verdict is Verdict.NEEDS_REVIEW
    assert decision.confidence == 0.62
    assert any("size 0.62 × coverage 1.00 × AI certainty 1.00" in r for r in decision.reasons)
    assert decision.facts["confidence"] == {"value": 0.62, "size": 0.625, "coverage": 1.0,
                                            "ai_certainty": 1.0}


def test_the_confidence_line_comes_from_the_policy():
    result = make_result(files=[make_file("main.go")])          # coverage 0.7
    assert decide(make_pull(), POLICY, make_outcome(result)).verdict is Verdict.NEEDS_REVIEW
    lenient = Policy(merge_min_confidence=0.7)
    assert decide(make_pull(), lenient, make_outcome(result)).verdict is Verdict.MERGE
