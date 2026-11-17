"""After the merge: how a merged pull request turned out.

Once GitHub reports a PR merged, review-app keeps an eye on it for a while (a week by default)
and records what happened:
- the CI on the merge commit, on the base branch;
- whether a later commit reverted it;
- whether later fixes went into the files it changed.

It sets that against what review-app had decided for the PR, so the dashboard can show how often
its calls held up. On GitHub it leaves one comment on the PR and keeps it up to date. Everything
here is a pure function; the worker does the fetching and the recording."""
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# the first look, once CI has had a moment to start; then while CI runs, and then daily
FIRST_LOOK = timedelta(minutes=10)
CI_RECHECK = timedelta(minutes=15)
DAILY_RECHECK = timedelta(days=1)
# limits on what one look fetches from GitHub
MAX_COMMITS = 200          # commits on the base branch since the merge
MAX_FILE_LOOKUPS = 15      # fix-looking commits whose files are fetched
MAX_PR_FILES = 300
MAX_LISTED = 5
TROUBLED = ("reverted", "ci_failing", "fixed_later")
COMMENT_MARKER = "<!-- review-app:outcome -->"

FIX_WORDS = re.compile(r"\b(?:fix(?:es|ed)?|hotfix|bug|broken|regression|repair(?:s|ed)?)\b", re.IGNORECASE)
REVERT_SUBJECT = re.compile(r'^Revert "(?P<title>.+)"', re.IGNORECASE)
REVERTS_COMMIT = re.compile(r"This reverts commit (?P<sha>[0-9a-f]{7,40})", re.IGNORECASE)


@dataclass(frozen=True)
class CheckResult:
    name: str
    state: str    # "passed" | "failed" | "pending" | "skipped"


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str
    date: str = ""                        # ISO, UTC
    files: tuple[str, ...] | None = None  # fetched only for commits that look like fixes

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0].strip()


@dataclass(frozen=True)
class CiSummary:
    state: str    # "passing" | "failing" | "pending" | "none"
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowUp:
    sha: str
    subject: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    assessment: str   # "watching" | "healthy" | "fixed_later" | "ci_failing" | "reverted"
    ci: CiSummary
    revert: CommitInfo | None = None
    follow_ups: tuple[FollowUp, ...] = field(default_factory=tuple)
    final: bool = False

    @property
    def troubled(self) -> bool:
        return self.assessment in TROUBLED


# ── CI on the merge commit ───────────────────────────────────────────────────────────────
def check_run_state(status: str | None, conclusion: str | None) -> str:
    """A check run's status and conclusion, as passed / failed / pending / skipped."""
    if status != "completed":
        return "pending"
    if conclusion == "success":
        return "passed"
    if conclusion in ("neutral", "skipped", "stale", None):
        return "skipped"
    return "failed"    # failure, timed_out, cancelled, action_required, startup_failure


def status_state(state: str | None) -> str:
    """A commit status (the older API) as passed / failed / pending."""
    return {"success": "passed", "pending": "pending"}.get(state or "", "failed")


def summarize_checks(results: Iterable[CheckResult], ignore: Sequence[str] = ()) -> CiSummary:
    by_state: dict[str, list[str]] = {"passed": [], "failed": [], "pending": []}
    for result in results:
        if result.name in ignore or result.state not in by_state:
            continue
        by_state[result.state].append(result.name)
    if by_state["failed"]:
        state = "failing"
    elif by_state["pending"]:
        state = "pending"
    elif by_state["passed"]:
        state = "passing"
    else:
        state = "none"
    return CiSummary(state, *(tuple(sorted(by_state[s])) for s in ("passed", "failed", "pending")))


# ── reverts and follow-up fixes ──────────────────────────────────────────────────────────
def find_revert(commits: Iterable[CommitInfo], merge_sha: str, repo: str, number: int,
                title: str) -> CommitInfo | None:
    """The commit that reverted the PR. It's recognised by the text git and GitHub write into a
    revert: "This reverts commit <the merge commit>", "Reverts owner/repo#N", or
    `Revert "<the PR's title>"`."""
    reverts_pr = re.compile(rf"\bReverts {re.escape(repo)}#{number}\b", re.IGNORECASE)
    for commit in commits:
        if commit.sha == merge_sha:
            continue
        reverted = REVERTS_COMMIT.search(commit.message)
        if reverted and merge_sha.lower().startswith(reverted["sha"].lower()):
            return commit
        if reverts_pr.search(commit.message):
            return commit
        subject = REVERT_SUBJECT.match(commit.subject)
        if subject and title and subject["title"].startswith(title.strip()):
            return commit
    return None


def looks_like_fix(message: str) -> bool:
    return bool(FIX_WORDS.search(message.split("\n", 1)[0]))


def follow_up_fixes(commits: Iterable[CommitInfo], pr_files: Iterable[str],
                    skip: Iterable[str] = ()) -> tuple[FollowUp, ...]:
    """Later fix-looking commits that changed files this PR changed."""
    changed = set(pr_files)
    skipped = set(skip)
    found = []
    for commit in commits:
        if commit.sha in skipped or commit.files is None or not looks_like_fix(commit.message):
            continue
        overlap = sorted(changed.intersection(commit.files))
        if overlap:
            found.append(FollowUp(commit.sha, commit.subject, tuple(overlap[:MAX_LISTED])))
    return tuple(found)


# ── the verdict on the merge, and on review-app's call ───────────────────────────────────
def assess(ci: CiSummary, revert: CommitInfo | None, follow_ups: Sequence[FollowUp],
           final: bool) -> str:
    if revert is not None:
        return "reverted"
    if ci.state == "failing":
        return "ci_failing"
    if follow_ups:
        return "fixed_later"
    return "healthy" if final else "watching"


def judge(verdict: str | None, assessment: str) -> str:
    """How review-app's call compares with what happened:
    - agreed: it said merge, and the merge held up;
    - warned: it said needs review or block, and the merge did run into trouble;
    - missed: it said merge, and the merge ran into trouble;
    - false_alarm: it said block, and the merge held up;
    - fine: it said needs review, and the merge held up;
    - pending: too early to tell;
    - not_reviewed: review-app made no decision for this PR."""
    if verdict is None:
        return "not_reviewed"
    if assessment == "watching":
        return "pending"
    troubled = assessment in TROUBLED
    if verdict == "merge":
        return "missed" if troubled else "agreed"
    if troubled:
        return "warned"
    return "false_alarm" if verdict == "block" else "fine"


def next_look(now: datetime, watch_until: datetime, ci_state: str) -> datetime | None:
    """When to look again, or None once the watch is over."""
    if now >= watch_until:
        return None
    wait = CI_RECHECK if ci_state == "pending" else DAILY_RECHECK
    return min(watch_until, now + wait)


# ── the comment on the PR ────────────────────────────────────────────────────────────────
ASSESSMENT_TEXT = {
    "watching": ("👀", "Healthy so far, still watching"),
    "healthy": ("✅", "Held up: no CI failure, revert or follow-up fix"),
    "fixed_later": ("🩹", "Fixed later: follow-up fixes went into its files"),
    "ci_failing": ("❌", "CI is failing on the merge commit"),
    "reverted": ("⏪", "Reverted"),
}
CALL_TEXT = {
    "agreed": "held up",
    "warned": "was right to hold back",
    "missed": "missed this",
    "false_alarm": "was too strict",
    "fine": "sent it to a person, and it held up",
    "pending": "too early to tell",
    "not_reviewed": "no decision recorded",
}
VERDICT_TEXT = {"merge": "Merge", "needs_review": "Needs review", "block": "Block"}


def ci_line(ci: CiSummary) -> str:
    if ci.state == "none":
        return "no checks reported"
    parts = []
    if ci.passed:
        parts.append(f"✅ {len(ci.passed)} passed")
    if ci.failed:
        parts.append(f"❌ {len(ci.failed)} failed: {', '.join(ci.failed[:MAX_LISTED])}")
    if ci.pending:
        parts.append(f"⏳ {len(ci.pending)} still running")
    return " · ".join(parts)


def report(outcome: Outcome, *, repo: str, merge_sha: str, verdict: str | None,
           confidence: float | None, call: str, watch_until: str, checked_at: str) -> str:
    """The Markdown for review-app's comment on the merged PR."""
    emoji, headline = ASSESSMENT_TEXT[outcome.assessment]
    commit_url = f"https://github.com/{repo}/commit/"
    if outcome.revert is None:
        revert = "no"
    else:
        revert = f"yes, in [`{outcome.revert.sha[:7]}`]({commit_url}{outcome.revert.sha}) {outcome.revert.subject}"
    if outcome.follow_ups:
        fixes = "; ".join(f"[`{f.sha[:7]}`]({commit_url}{f.sha}) {f.subject} ({', '.join(f.files)})"
                          for f in outcome.follow_ups[:MAX_LISTED])
    else:
        fixes = "none"
    if verdict is None:
        decided = "no decision recorded"
    else:
        sure = f", confidence {confidence:.2f}" if confidence is not None else ""
        decided = f"{VERDICT_TEXT.get(verdict, verdict)}{sure}: {CALL_TEXT[call]}"
    watch = ("final" if outcome.final
             else f"review-app keeps watching until {watch_until[:10]}")
    return "\n".join([
        "### review-app: after the merge",
        "",
        f"**{emoji} {headline}**",
        "",
        "| | |",
        "|---|---|",
        f"| CI on the merge commit [`{merge_sha[:7]}`]({commit_url}{merge_sha}) | {ci_line(outcome.ci)} |",
        f"| Reverted | {revert} |",
        f"| Follow-up fixes to its files | {fixes} |",
        f"| review-app's call | {decided} |",
        "",
        f"<sub>Checked {checked_at[:16].replace('T', ' ')} UTC · {watch}. This comment is updated in place.</sub>",
        COMMENT_MARKER,
    ])
