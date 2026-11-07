"""The decision engine: from an analysis, the PR's facts and the repository's policy to merge,
needs review or block. Pure functions, no I/O; actions.py carries the decision out.

1. Block on blocking findings, or a score or grade under the policy's block thresholds.
2. Needs review if any merge gate fails: an incomplete or failed analysis, a fork, a draft, a base
   branch auto-merge may not use, a path that always needs a human, a missing AI review, or a
   score, grade or confidence under the merge thresholds.
3. Merge otherwise.
"""
import os
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from pr_analyzer.analyzer.ast_analyzer import language_name
from pr_analyzer.llm.engine import LLM_RULE
from pr_analyzer.models import FileChange, Suggestion
from pr_analyzer.project_config import CONFIG_FILE, severity_rank
from pr_analyzer.security.dep_auditor import DEPENDENCY_AUDIT_RULE
from pr_analyzer.security.semgrep_runner import SEMGREP_RULE

from review_app.models import AnalysisOutcome, PullInfo
from review_app.policy import Policy, grade_rank

# The only pr-analyzer warnings that don't make an analysis incomplete. Anything else, including
# warnings worded differently or added by later versions, counts as skipped work: fail safe.
BENIGN_WARNINGS = (
    "Could not post the GitHub review",   # about reporting the result, not producing it
)
# the scanners every merge needs
REQUIRED_STAGES = (SEMGREP_RULE, DEPENDENCY_AUDIT_RULE)

# confidence: the size factor is 1.0 up to FULL_CONFIDENCE_LINES changed lines and falls linearly
# to MIN_SIZE_FACTOR at FLOOR_CONFIDENCE_LINES: reviews catch less on large changes
FULL_CONFIDENCE_LINES = 400
FLOOR_CONFIDENCE_LINES = 2000
MIN_SIZE_FACTOR = 0.5
# lines outside the languages pr-analyzer parses fully (and docs) only get the regex rules,
# Semgrep and the AI review
MIN_COVERAGE_FACTOR = 0.7
DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt", ".adoc"})
# each MEDIUM-or-worse AI finding the model is unsure of costs this much, down to the floor
UNSURE_AI_CONFIDENCE = 0.8
UNSURE_PENALTY = 0.1
MIN_CERTAINTY_FACTOR = 0.6
MAX_NAMED = 5


class Verdict(StrEnum):
    MERGE = "merge"
    NEEDS_REVIEW = "needs_review"
    BLOCK = "block"


@dataclass(frozen=True)
class Confidence:
    value: float
    size: float
    coverage: float
    ai_certainty: float


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    confidence: float
    complete: bool                    # the analysis covered everything
    reasons: tuple[str, ...]
    facts: dict[str, Any] = field(default_factory=dict)   # what the decision was made from


def named(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_NAMED])
    return shown + (f" and {len(names) - MAX_NAMED} more" if len(names) > MAX_NAMED else "")


# ── completeness ─────────────────────────────────────────────────────────────────────────
def completeness_problems(outcome: AnalysisOutcome, pull: PullInfo, policy: Policy) -> list[str]:
    """Everything the analysis skipped or couldn't do; empty when it was complete."""
    result = outcome.result
    problems = [f"pr-analyzer: {w}" for w in result.warnings if not w.startswith(BENIGN_WARNINGS)]

    if outcome.changed_files is None:
        problems.append("the PR's files couldn't be listed to check that none was skipped")
    else:
        analysed = {f.filename for f in result.pr.files}
        # GitHub lists binary files and very large diffs without a patch, and pr-analyzer drops
        # them; a rename with no changes is the one such file with nothing in it to analyse
        unanalysed = [
            f.filename for f in outcome.changed_files
            if f.filename not in analysed and not (f.status == "renamed" and f.changes == 0)
        ]
        if unanalysed:
            problems.append(f"{len(unanalysed)} changed file(s) came without a diff (binary, or "
                            f"too big to diff), so nothing checked them: {named(unanalysed)}")
        if pull.changed_files > len(outcome.changed_files):
            problems.append(f"GitHub listed only {len(outcome.changed_files)} of the "
                            f"{pull.changed_files} changed files")

    unfetched = [f.filename for f in result.pr.files
                 if f.status != "removed" and f.patch and f.raw_content is None]
    if unfetched:
        problems.append(f"the full content of {len(unfetched)} file(s) couldn't be fetched, so "
                        f"only their diffs were checked: {named(unfetched)}")
    problems += [f"{tool} isn't installed where the app runs" for tool in outcome.missing_tools]

    required = REQUIRED_STAGES + ((LLM_RULE,) if policy.require_llm_review else ())
    problems += [f"the {stage} check is switched off in the project's {CONFIG_FILE}"
                 for stage in outcome.disabled_stages if stage in required]
    if outcome.head_moved:
        problems.append("new commits arrived while the analysis ran")
    return problems


# ── confidence ───────────────────────────────────────────────────────────────────────────
def size_factor(changed_lines: int) -> float:
    if changed_lines <= FULL_CONFIDENCE_LINES:
        return 1.0
    if changed_lines >= FLOOR_CONFIDENCE_LINES:
        return MIN_SIZE_FACTOR
    share = (changed_lines - FULL_CONFIDENCE_LINES) / (FLOOR_CONFIDENCE_LINES - FULL_CONFIDENCE_LINES)
    return 1.0 - (1.0 - MIN_SIZE_FACTOR) * share


def coverage_factor(files: list[FileChange]) -> float:
    total = sum(f.additions + f.deletions for f in files)
    if total == 0:
        return 1.0
    deep = sum(
        f.additions + f.deletions for f in files
        if language_name(f.filename) or os.path.splitext(f.filename)[1].lower() in DOC_EXTENSIONS
    )
    return MIN_COVERAGE_FACTOR + (1.0 - MIN_COVERAGE_FACTOR) * deep / total


def unsure_ai_findings(suggestions: list[Suggestion]) -> list[Suggestion]:
    return [s for s in suggestions
            if s.source == "llm" and severity_rank(s.severity) >= severity_rank("MEDIUM")
            and s.confidence < UNSURE_AI_CONFIDENCE]


def ai_certainty_factor(suggestions: list[Suggestion]) -> float:
    return max(MIN_CERTAINTY_FACTOR, 1.0 - UNSURE_PENALTY * len(unsure_ai_findings(suggestions)))


def compute_confidence(outcome: AnalysisOutcome) -> Confidence:
    pr = outcome.result.pr
    size = size_factor(pr.total_additions + pr.total_deletions)
    coverage = coverage_factor(pr.files)
    certainty = ai_certainty_factor(outcome.result.suggestions)
    return Confidence(value=round(size * coverage * certainty, 2), size=round(size, 3),
                      coverage=round(coverage, 3), ai_certainty=round(certainty, 3))


# ── the rules ────────────────────────────────────────────────────────────────────────────
def block_reasons(outcome: AnalysisOutcome, policy: Policy) -> list[str]:
    result = outcome.result
    score = result.score
    reasons = []
    if not score.passed:
        worst = sorted(result.suggestions, key=lambda s: -severity_rank(s.severity))[:3]
        examples = "; ".join(
            f"{s.severity} {s.title} ({s.filename}{f':{s.line}' if s.line else ''})" for s in worst
        )
        reasons.append(f"pr-analyzer found blocking issues ({score.critical_count} critical, "
                       f"{score.high_count} high): {examples}")
    if score.overall_score < policy.block_below_score:
        reasons.append(f"Score {score.overall_score:.1f} is under block_below_score "
                       f"({policy.block_below_score:g})")
    if grade_rank(score.grade) < grade_rank(policy.block_below_grade):
        reasons.append(f"Grade {score.grade} is under block_below_grade ({policy.block_below_grade})")
    return reasons


def pr_gate_reasons(pull: PullInfo, policy: Policy, paths: list[str]) -> list[str]:
    reasons = []
    if pull.state != "open":
        reasons.append(f"It is {pull.state}, not open")
    if pull.is_fork:
        reasons.append("It comes from a fork, and fork PRs are never merged automatically")
    if pull.is_draft:
        reasons.append("It is a draft")
    if not policy.allows_base(pull.base_branch, pull.default_branch):
        allowed = ", ".join(policy.base_branches) or f"{pull.default_branch}, the default branch"
        reasons.append(f"It targets {pull.base_branch}, and auto-merge only uses {allowed}")
    hits = policy.review_path_hits(paths)
    if hits:
        reasons.append(f"It changes paths that always need a human: {named(hits)}")
    return reasons


def changed_paths(outcome: AnalysisOutcome) -> list[str]:
    if outcome.changed_files is None:
        return [f.filename for f in outcome.result.pr.files]
    paths = []
    for f in outcome.changed_files:
        paths.append(f.filename)
        if f.previous_filename:   # moving a file out of a guarded path changes that path too
            paths.append(f.previous_filename)
    return paths


def pull_facts(pull: PullInfo) -> dict[str, Any]:
    return {"head_sha": pull.head_sha, "base_branch": pull.base_branch,
            "default_branch": pull.default_branch, "is_fork": pull.is_fork,
            "is_draft": pull.is_draft, "changed_files": pull.changed_files}


def decide(pull: PullInfo, policy: Policy, outcome: AnalysisOutcome | None = None,
           error: str | None = None) -> Decision:
    facts: dict[str, Any] = {"policy": policy.model_dump(mode="json"), "pull": pull_facts(pull)}
    if outcome is None:
        reasons = [f"The analysis didn't finish: {error or 'no result'}"]
        reasons += pr_gate_reasons(pull, policy, [])
        return Decision(Verdict.NEEDS_REVIEW, 0.0, False, tuple(reasons), facts)

    result = outcome.result
    score = result.score
    problems = completeness_problems(outcome, pull, policy)
    confidence = compute_confidence(outcome)
    facts.update(
        score=score.overall_score, grade=score.grade, passed=score.passed,
        critical=score.critical_count, high=score.high_count, findings=len(result.suggestions),
        llm_provider=result.llm_provider_used, confidence=asdict(confidence), problems=problems,
    )

    blocks = block_reasons(outcome, policy)
    if blocks:
        if problems:
            blocks.append(f"The analysis was also incomplete, so there may be more: "
                          f"{named(problems)}")
        return Decision(Verdict.BLOCK, confidence.value, not problems, tuple(blocks), facts)

    reasons = [f"The analysis is incomplete: {problem}" for problem in problems]
    reasons += pr_gate_reasons(pull, policy, changed_paths(outcome))
    if (policy.require_llm_review and not result.llm_provider_used
            and LLM_RULE not in outcome.disabled_stages):
        reasons.append("The AI review didn't run, and this repository's policy requires it")
    if score.overall_score < policy.merge_min_score:
        reasons.append(f"Score {score.overall_score:.1f} is under merge_min_score "
                       f"({policy.merge_min_score:g})")
    if grade_rank(score.grade) < grade_rank(policy.merge_min_grade):
        reasons.append(f"Grade {score.grade} is under merge_min_grade ({policy.merge_min_grade})")
    if confidence.value < policy.merge_min_confidence:
        reasons.append(
            f"Confidence {confidence.value:.2f} is under merge_min_confidence "
            f"({policy.merge_min_confidence:g}): size {confidence.size:.2f} × coverage "
            f"{confidence.coverage:.2f} × AI certainty {confidence.ai_certainty:.2f}"
        )
    if reasons:
        return Decision(Verdict.NEEDS_REVIEW, confidence.value, not problems, tuple(reasons), facts)

    cleared = (f"Score {score.overall_score:.1f} ({score.grade}) and confidence "
               f"{confidence.value:.2f} clear this repository's merge thresholds, and the "
               f"analysis was complete")
    return Decision(Verdict.MERGE, confidence.value, True, (cleared,), facts)
