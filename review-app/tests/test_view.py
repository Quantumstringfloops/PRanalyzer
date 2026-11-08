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
    active_filters,
    ago,
    area_chart,
    clock,
    confidence_tone,
    daily_counts,
    day_label,
    filter_findings,
    findings_summary,
    grade_tone,
    group_findings,
    hotspots,
    monotone_path,
    percent,
    query_string,
    ring,
    score_tone,
    shares,
    track_findings,
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


def test_daily_counts_fill_in_quiet_days():
    days = daily_counts({"2026-11-08": 3, "2026-11-05": 1, "2026-10-01": 9}, days=4,
                        today=NOW.date())
    assert [(d.isoformat(), n) for d, n in days] == [
        ("2026-11-05", 1), ("2026-11-06", 0), ("2026-11-07", 0), ("2026-11-08", 3)]


def test_the_area_chart_stays_inside_its_box():
    chart = area_chart([0, 3, 0, 1, 6, 6, 0], width=600, height=160, pad=14)
    xs = [x for x, _ in chart["points"]]
    ys = [y for _, y in chart["points"]]
    assert xs[0] == 0 and xs[-1] == 600 and chart["top"] == 6
    assert min(ys) == 14 and max(ys) == 146            # the peak at the top, zero on the baseline
    # monotone curves never swing past a point, so every control point stays in the box too
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", chart["line"])]
    assert all(14 <= y <= 146 for y in numbers[1::2])
    assert chart["area"].endswith("L600 160 L0 160 Z")


def test_charts_of_nothing():
    flat = area_chart([0, 0, 0])
    assert {y for _, y in flat["points"]} == {flat["baseline"]}
    assert area_chart([])["line"] == "" and monotone_path([(1, 2)]) == "M1 2"


def test_the_gauge_ring():
    assert ring(50) == {"r": 52.0, "c": 326.73, "dash": 163.36}
    assert ring(None)["dash"] == 0 and ring(250)["dash"] == ring(100)["c"]


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


def run(pr_id, findings, state="open", number=7, at="2026-11-08T10:00:00.000000Z"):
    return {"pr_id": pr_id, "repo": "octo/app", "number": number, "pr_title": "A PR",
            "author": "mona", "pr_state": state, "created_at": at, "findings": findings}


def test_findings_are_tracked_across_a_prs_commits():
    password = finding("CRITICAL", title="Hardcoded password", line=4)
    tracked = track_findings([
        run(1, [password, finding("LOW", line=3), finding("LOW", line=9)]),
        # the next commit: the password is gone, one magic number moved, one went away
        run(1, [finding("LOW", line=5)], at="2026-11-08T11:00:00.000000Z"),
        run(2, [finding("HIGH", title="Token logged")], state="merged", number=8),
    ])
    states = sorted((f["title"], f["line"], f["state"]) for f in tracked)
    assert states == [("Hardcoded password", 4, "resolved"), ("Magic number", 5, "pending"),
                      ("Magic number", 9, "resolved"), ("Token logged", 3, "ignored")]
    first = next(f for f in tracked if f["line"] == 5)
    assert first["first_seen"] == "2026-11-08T10:00:00.000000Z"        # the same finding, moved
    assert [f["severity"] for f in tracked][:2] == ["CRITICAL", "HIGH"]  # worst first


def test_the_findings_summary():
    tracked = track_findings([run(1, [finding("CRITICAL", title="A")]),
                              run(1, [], at="2026-11-08T11:00:00.000000Z"),
                              run(2, [finding("HIGH", title="B"), finding("LOW")], number=8)])
    assert findings_summary(tracked, pr_count=2) == {"high": 2, "resolved_pct": 50, "per_pr": 1}
    assert findings_summary([], pr_count=0) == {"high": 0, "resolved_pct": 0, "per_pr": 0}


def test_filtering_findings():
    tracked = track_findings([run(1, [finding("HIGH", title="SQL injection", filename="db.py"),
                                      {**finding("LOW"), "source": "llm"}])])
    assert [f["title"] for f in filter_findings(tracked, severity="high")] == ["SQL injection"]
    assert [f["title"] for f in filter_findings(tracked, source="LLM")] == ["Magic number"]
    assert [f["title"] for f in filter_findings(tracked, q="db.py")] == ["SQL injection"]
    assert filter_findings(tracked, repo="octo/other") == []


def test_hotspots_rank_files_by_open_findings():
    tracked = track_findings([
        run(1, [finding("HIGH", filename="src/db.py"), finding("LOW", filename="src/db.py", line=8),
                finding("LOW", filename="README.md")]),
        run(2, [finding("CRITICAL", title="Gone", filename="src/db.py")], number=8),
        run(2, [], number=8, at="2026-11-08T11:00:00.000000Z"),     # ...resolved, so not counted
    ])
    spots = hotspots(tracked)
    assert [(s["filename"], s["total"], s["width"]) for s in spots] == [
        ("src/db.py", 2, 100), ("README.md", 1, 50)]
    assert spots[0]["folder"] == "src" and spots[0]["name"] == "db.py"
    assert [(severity, n) for severity, n, _ in spots[0]["parts"]] == [("HIGH", 1), ("LOW", 1)]


def test_filter_links_keep_the_other_filters():
    params = {"days": 30, "severity": "high", "state": "", "type": "llm", "q": "token"}
    assert query_string(params, days=7) == "days=7&severity=high&type=llm&q=token"
    chips = active_filters(params)
    assert [label for label, _ in chips] == ["Severity: high", "Type: AI review", "Search: token"]
    assert chips[0][1] == "days=30&type=llm&q=token"


def test_different_findings_stay_apart():
    groups = group_findings([finding("LOW", description="Name it."),
                             finding("LOW", description="Something else.")])
    assert [f["count"] for f in groups[0]["findings"]] == [1, 1]
