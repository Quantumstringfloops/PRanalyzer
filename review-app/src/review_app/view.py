"""What the dashboard's templates need beyond the database rows: labels, tones, relative times,
groupings, and the geometry of its SVG charts. Pure functions, so they're tested without a web
app. (The CSP allows no inline styles, so every shape is computed here and drawn with SVG
attributes.)"""
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
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


# ── charts ───────────────────────────────────────────────────────────────────────────────
def daily_counts(counts: Mapping[str, int], days: int = 14,
                 today: date | None = None) -> list[tuple[date, int]]:
    """A count for each of the last `days` days, oldest first, from {"2026-11-07": 3}."""
    last = today or datetime.now(UTC).date()
    span = [last - timedelta(days=back) for back in range(days - 1, -1, -1)]
    return [(day, int(counts.get(day.isoformat(), 0))) for day in span]


def area_chart(values: list[int], width: float = 600, height: float = 160,
               pad: float = 14) -> dict[str, Any]:
    """An SVG area chart's geometry: a smooth line through the values (monotone, so it never
    swings past a point), the area under it, and each point."""
    top = max(values, default=0) or 1
    step = width / max(len(values) - 1, 1)
    points = [(round(i * step, 1), round(height - pad - v / top * (height - 2 * pad), 1))
              for i, v in enumerate(values)]
    line = monotone_path(points)
    area = (f"{line} L{num(points[-1][0])} {num(height)} L{num(points[0][0])} {num(height)} Z"
            if points else "")
    return {"line": line, "area": area, "points": points, "top": top, "width": width,
            "height": height, "baseline": height - pad}


def num(value: float) -> str:
    """A coordinate for an SVG path: 600 rather than 600.0, 46.2 rather than 46.15384."""
    return f"{round(value, 1):g}"


def monotone_path(points: list[tuple[float, float]]) -> str:
    """An SVG path through `points` with Fritsch–Carlson monotone cubic curves."""
    if not points:
        return ""
    if len(points) == 1:
        return f"M{num(points[0][0])} {num(points[0][1])}"
    xs, ys = zip(*points, strict=True)
    deltas = [(ys[i + 1] - ys[i]) / ((xs[i + 1] - xs[i]) or 1) for i in range(len(xs) - 1)]
    slopes = [deltas[0]]
    slopes += [0.0 if deltas[i - 1] * deltas[i] <= 0 else (deltas[i - 1] + deltas[i]) / 2
               for i in range(1, len(deltas))]
    slopes.append(deltas[-1])
    for i, delta in enumerate(deltas):
        if delta == 0:
            slopes[i] = slopes[i + 1] = 0.0
            continue
        a, b = slopes[i] / delta, slopes[i + 1] / delta
        if a * a + b * b > 9:
            t = 3 / math.hypot(a, b)
            slopes[i], slopes[i + 1] = t * a * delta, t * b * delta
    path = [f"M{num(xs[0])} {num(ys[0])}"]
    for i in range(len(xs) - 1):
        third = (xs[i + 1] - xs[i]) / 3
        path.append(f"C{num(xs[i] + third)} {num(ys[i] + slopes[i] * third)} "
                    f"{num(xs[i + 1] - third)} {num(ys[i + 1] - slopes[i + 1] * third)} "
                    f"{num(xs[i + 1])} {num(ys[i + 1])}")
    return " ".join(path)


def ring(value: float | None, top: float = 100.0, radius: float = 52.0) -> dict[str, float]:
    """A circular gauge: the circle's length, and how much of it the value fills."""
    circumference = 2 * math.pi * radius
    share = 0.0 if value is None or top <= 0 else max(0.0, min(1.0, value / top))
    return {"r": radius, "c": round(circumference, 2), "dash": round(circumference * share, 2)}


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
