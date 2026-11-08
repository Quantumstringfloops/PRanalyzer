"""The dashboard's presentation helpers: times, widths, tones and the grouping of findings."""
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import review_app
from review_app.policy import Policy
from review_app.view import (
    action_label,
    action_tone,
    ago,
    clock,
    confidence_tone,
    day_label,
    grade_tone,
    group_findings,
    percent,
    score_tone,
    shares,
    when,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def stamp(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.mark.parametrize(("delta", "text"), [
    (timedelta(seconds=-10), "just now"),
    (timedelta(minutes=-5), "5 min ago"),
    (timedelta(minutes=-89), "89 min ago"),
    (timedelta(hours=-3), "3 h ago"),
    (timedelta(hours=-35), "35 h ago"),
    (timedelta(days=-3), "3 d ago"),
    (timedelta(minutes=10), "in 10 min"),
    (timedelta(days=-45), "24 Sep 2026"),
])
def test_ago(delta, text):
    assert ago(stamp(delta), now=NOW) == text


@pytest.mark.parametrize("bad", [None, "", "yesterday"])
def test_ago_without_a_time(bad):
    assert ago(bad, now=NOW) == ""


def test_day_labels():
    assert day_label(stamp(timedelta(hours=-1)), now=NOW) == "Today"
    assert day_label(stamp(timedelta(days=-1)), now=NOW) == "Yesterday"
    assert day_label(stamp(timedelta(days=-2)), now=NOW) == "Fri 6 Nov 2026"
    assert day_label(None, now=NOW) == ""


def test_exact_times():
    assert when("2026-11-07T20:59:48.123456Z") == "2026-11-07 20:59:48 UTC"
    assert clock("2026-11-07T20:59:48.123456Z") == "20:59:48 UTC"
    assert when(None) == clock(None) == ""


@pytest.mark.parametrize(("value", "top", "expected"), [
    (59.4, 100, 59), (100, 100, 100), (130, 100, 100), (-5, 100, 0), (None, 100, 0),
    (0.82, 1, 82), (1, 0, 0),
])
def test_percent(value, top, expected):
    assert percent(value, top) == expected


def test_every_width_a_template_can_ask_for_exists():
    # the CSP allows no inline styles, so a bar's width is one of these classes
    css = (Path(review_app.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    widths = {int(n) for n in re.findall(r"\.w(\d+) \{ width: \1%; \}", css)}
    assert widths == set(range(101))


@pytest.mark.parametrize(("counts", "expected"), [
    ({"merge": 1, "needs_review": 1, "block": 1}, [34, 33, 33]),
    ({"merge": 0, "needs_review": 1, "block": 1}, [0, 50, 50]),
    ({"merge": 0, "needs_review": 0, "block": 0}, [0, 0, 0]),
    ({"merge": 1, "needs_review": 299, "block": 0}, [1, 99, 0]),   # never drawn at 0%
])
def test_shares_add_up_to_a_whole(counts, expected):
    parts = shares(counts)
    assert [pct for _, _, pct in parts] == expected
    assert [(name, n) for name, n, _ in parts] == list(counts.items())
    assert sum(expected) in (0, 100)


def test_tones_follow_the_repositorys_lines():
    policy = Policy()   # merge from 90 and A-, block under 65 and below D, confidence 0.8
    assert [score_tone(s, policy) for s in (95, 90, 89.9, 65, 64.9, None)] == [
        "good", "good", "warning", "warning", "critical", "none"]
    assert [grade_tone(g, policy) for g in ("A+", "A-", "B+", "D", "F", "?")] == [
        "good", "good", "warning", "warning", "critical", "none"]
    assert [confidence_tone(c, policy) for c in (0.8, 0.79, None)] == ["good", "warning", "none"]
    strict = Policy(merge_min_score=95, block_below_score=80)
    assert score_tone(90, strict) == "warning" and score_tone(79, strict) == "critical"


def test_audit_actions():
    assert action_label("check_run.created") == "Check run created"
    assert action_tone("merge.succeeded") == "good"
    assert action_tone("merge.failed") == action_tone("merge.gave_up") == "serious"
    assert action_tone("decision.made", {"verdict": "block"}) == "critical"
    assert action_tone("decision.made", {"verdict": "merge"}) == "good"
    assert action_tone("job.queued") == action_tone("decision.made") == "neutral"


def finding(severity, title="Magic number", line=3, filename="a.py", description="Name it."):
    return {"severity": severity, "title": title, "line": line, "filename": filename,
            "description": description, "source": "static", "confidence": 1.0}


def test_findings_are_grouped_worst_first_and_repeats_folded():
    groups = group_findings([
        finding("LOW", line=3), finding("LOW", line=9), finding("LOW", line=4, filename="b.py"),
        finding("CRITICAL", title="Hardcoded password"), finding("weird", title="Odd"),
        finding("medium", title="Too complex", line=None),
    ])
    assert [(g["severity"], g["count"]) for g in groups] == [
        ("CRITICAL", 1), ("MEDIUM", 1), ("LOW", 3), ("WEIRD", 1)]
    low = groups[2]["findings"]
    assert len(low) == 1 and low[0]["count"] == 3
    assert low[0]["files"] == {"a.py": [3, 9], "b.py": [4]}
    assert groups[1]["findings"][0]["files"] == {"a.py": []}   # no line, just the file


def test_different_findings_stay_apart():
    groups = group_findings([finding("LOW", description="Name it."),
                             finding("LOW", description="Something else.")])
    assert [f["count"] for f in groups[0]["findings"]] == [1, 1]
