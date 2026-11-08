"""What the dashboard's templates need beyond the database rows: labels, tones, relative times and
groupings. Pure functions, so they're tested without a web app."""
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from review_app.policy import Policy, grade_rank

VERDICT_LABELS = {"merge": "Merge", "needs_review": "Needs review", "block": "Block"}
VERDICT_TONES = {"merge": "good", "needs_review": "warning", "block": "critical"}
OUTCOME_LABELS = {
    "merge_recommended": "Merge recommended",
    "merged": "Merged",
    "merge_waiting": "Waiting for checks",
    "merge_failed": "Merge failed",
    "merge_refused": "Merge refused",
    "merge_aborted": "Merge aborted",
    "needs_review": "Left for review",
    "review_requested": "Reviewers requested",
    "review_request_failed": "Review request failed",
    "blocked": "Blocked",
}
OUTCOME_TONES = {
    "merge_recommended": "good",
    "merged": "good",
    "merge_waiting": "warning",
    "needs_review": "warning",
    "review_requested": "warning",
    "merge_failed": "serious",
    "merge_refused": "serious",
    "merge_aborted": "serious",
    "review_request_failed": "serious",
    "blocked": "critical",
}
# what an outcome meant, in a sentence, for the PR page
OUTCOME_NOTES = {
    "merge_recommended": "Recommend-only mode, so nothing was merged.",
    "merged": "Merged through GitHub's merge API, pinned to the analysed commit.",
    "merge_waiting": "GitHub is waiting for required checks; the merge is tried again.",
    "merge_failed": "The merge call failed, so a person should look.",
    "merge_refused": "GitHub refused the merge.",
    "merge_aborted": "Something changed after the analysis, so the merge was called off.",
    "needs_review": "Left for a person to review.",
    "review_requested": "Reviewers were asked to look.",
    "review_request_failed": "Asking for reviewers failed.",
    "blocked": "Blocked until a new commit passes.",
}
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
SEVERITY_TONES = {"CRITICAL": "critical", "HIGH": "serious", "MEDIUM": "warning"}
JOB_TONES = {"queued": "neutral", "running": "info", "done": "good", "failed": "critical"}
FAILED_ACTIONS = ("failed", "refused", "aborted", "gave_up")


# ── times ────────────────────────────────────────────────────────────────────────────────
def parse_stamp(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def when(text: str | None) -> str:
    """A stored stamp to the second, '2026-11-07 20:59:48 UTC'."""
    return (text or "")[:19].replace("T", " ") + (" UTC" if text else "")


def clock(text: str | None) -> str:
    """The time of day of a stored stamp, '20:59:48 UTC'."""
    return f"{text[11:19]} UTC" if text and len(text) >= 19 else ""


def ago(text: str | None, now: datetime | None = None) -> str:
    """'just now', '5 min ago', 'in 2 h', '3 d ago', or the date once it's a month away."""
    moment = parse_stamp(text)
    if moment is None:
        return ""
    seconds = ((now or datetime.now(UTC)) - moment).total_seconds()
    future, seconds = seconds < 0, abs(seconds)
    if seconds < 45:
        return "just now"
    for limit, size, unit in ((90 * 60, 60, "min"), (36 * 3600, 3600, "h"),
                              (30 * 86400, 86400, "d")):
        if seconds < limit:
            n = max(1, round(seconds / size))
            return f"in {n} {unit}" if future else f"{n} {unit} ago"
    return f"{moment.day} {moment:%b %Y}"


def day_label(text: str | None, now: datetime | None = None) -> str:
    """'Today', 'Yesterday' or 'Sat 7 Nov 2026', for headings over a day's audit entries (UTC)."""
    moment = parse_stamp(text)
    if moment is None:
        return ""
    days = ((now or datetime.now(UTC)).astimezone(UTC).date() - moment.astimezone(UTC).date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{moment:%a} {moment.day} {moment:%b %Y}"


# ── numbers ──────────────────────────────────────────────────────────────────────────────
def percent(value: float | None, top: float = 100.0) -> int:
    """value as a whole percentage of top, within 0–100: the w0–w100 width classes.
    (The page's CSP allows no inline styles, so widths come from classes.)"""
    if value is None or top <= 0:
        return 0
    return max(0, min(100, round(value / top * 100)))


def shares(counts: Mapping[str, int]) -> list[tuple[str, int, int]]:
    """(name, count, percent) per part, the percents adding up to exactly 100 by largest
    remainder. A part with any count gets at least 1%, so it never vanishes from the bar."""
    total = sum(counts.values())
    if total <= 0:
        return [(name, n, 0) for name, n in counts.items()]
    exact = {name: n * 100 / total for name, n in counts.items()}
    whole = {name: int(value) for name, value in exact.items()}
    left = 100 - sum(whole.values())
    for name in sorted(counts, key=lambda k: exact[k] - whole[k], reverse=True)[:left]:
        whole[name] += 1
    for name, n in counts.items():
        if n and not whole[name]:
            biggest = max(whole, key=lambda k: whole[k])
            whole[biggest] -= 1
            whole[name] = 1
    return [(name, n, whole[name]) for name, n in counts.items()]


# ── tones: good / warning / serious / critical, always shown with an icon or a label ─────
def score_tone(score: float | None, policy: Policy) -> str:
    """Where a score falls against the repository's merge and block lines."""
    if score is None:
        return "none"
    if score >= policy.merge_min_score:
        return "good"
    if score < policy.block_below_score:
        return "critical"
    return "warning"


def grade_tone(grade: str | None, policy: Policy) -> str:
    rank = grade_rank(grade or "")
    if rank < 0:
        return "none"
    if rank >= grade_rank(policy.merge_min_grade):
        return "good"
    if rank < grade_rank(policy.block_below_grade):
        return "critical"
    return "warning"


def confidence_tone(confidence: float | None, policy: Policy) -> str:
    if confidence is None:
        return "none"
    return "good" if confidence >= policy.merge_min_confidence else "warning"


def action_tone(action: str, details: Mapping[str, Any] | None = None) -> str:
    if action == "decision.made":
        return VERDICT_TONES.get(str((details or {}).get("verdict", "")), "neutral")
    if action == "merge.succeeded":
        return "good"
    if action.endswith(FAILED_ACTIONS):
        return "serious"
    if action in ("merge.waiting", "job.retrying"):
        return "warning"
    return "neutral"


def action_label(action: str) -> str:
    """'check_run.created' → 'Check run created'."""
    text = action.replace(".", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


# ── findings ─────────────────────────────────────────────────────────────────────────────
def group_findings(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Findings by severity, worst first. The same finding in several places (same title,
    description and source) is listed once, with its places by file."""
    groups: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for f in findings:
        severity = str(f.get("severity") or "").upper() or "OTHER"
        key = (f.get("title"), f.get("description"), f.get("source"))
        entry = groups.setdefault(severity, {}).setdefault(
            key, {**f, "severity": severity, "files": {}, "count": 0})
        lines = entry["files"].setdefault(f.get("filename") or "", [])
        if f.get("line") is not None:
            lines.append(f["line"])
        entry["count"] += 1
    order = [s for s in SEVERITIES if s in groups] + sorted(s for s in groups if s not in SEVERITIES)
    return [{"severity": severity,
             "findings": list(groups[severity].values()),
             "count": sum(entry["count"] for entry in groups[severity].values())}
            for severity in order]
