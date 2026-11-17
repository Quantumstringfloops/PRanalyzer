"""After the merge: how a merged PR turned out, and the worker's looks at it."""
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    MERGE_SHA,
    SHA,
    FakeAccess,
    FakeAnalyzer,
    FakeGateway,
    audit_actions,
    make_pull,
    merged_pull,
)
from github import GithubException

from review_app.outcomes import (
    COMMENT_MARKER,
    CheckResult,
    CiSummary,
    CommitInfo,
    FollowUp,
    Outcome,
    assess,
    check_run_state,
    find_revert,
    follow_up_fixes,
    judge,
    looks_like_fix,
    next_look,
    report,
    status_state,
    summarize_checks,
)
from review_app.policy import Policy, PolicyBook
from review_app.worker import Worker

MERGED = datetime(2026, 11, 8, 15, 57, 51, tzinfo=UTC)
SOON = MERGED + timedelta(minutes=12)
LATER = MERGED + timedelta(days=8)       # past the 7-day watch
PASSING = [CheckResult("test (3.12)", "passed"), CheckResult("review-app", "failed")]
FAILING = [CheckResult("test (3.12)", "failed"), CheckResult("lint", "passed")]


# ── CI on the merge commit ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "conclusion", "state"), [
    ("completed", "success", "passed"), ("completed", "failure", "failed"),
    ("completed", "timed_out", "failed"), ("completed", "cancelled", "failed"),
    ("completed", "neutral", "skipped"), ("completed", "skipped", "skipped"),
    ("in_progress", None, "pending"), ("queued", None, "pending"),
])
def test_check_run_states(status, conclusion, state):
    assert check_run_state(status, conclusion) == state


def test_commit_status_states():
    assert [status_state(s) for s in ("success", "pending", "failure", "error")] == [
        "passed", "pending", "failed", "failed"]


def test_the_ci_summary_leaves_review_app_out():
    assert summarize_checks(PASSING, ignore=("review-app",)) == CiSummary(
        "passing", passed=("test (3.12)",))
    assert summarize_checks(FAILING).state == "failing"
    assert summarize_checks([CheckResult("build", "pending"),
                             CheckResult("lint", "passed")]).state == "pending"
    assert summarize_checks([CheckResult("docs", "skipped")]).state == "none"


# ── reverts and follow-up fixes ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("message", [
    f'Revert "Add a helper"\n\nThis reverts commit {MERGE_SHA}.',
    f"Undo it\n\nThis reverts commit {MERGE_SHA[:10]}.",
    'Revert "Add a helper" (#9)\n\nReverts octo/app#7',
    'Revert "Add a helper (#7)"',
])
def test_a_revert_is_recognised(message):
    revert = CommitInfo("r" * 40, message)
    commits = [CommitInfo("x" * 40, "Unrelated work"), revert]
    assert find_revert(commits, MERGE_SHA, "octo/app", 7, "Add a helper") == revert


@pytest.mark.parametrize("message", [
    "This reverts commit 0123456789abcdef.",       # some other commit
    "Reverts octo/app#70",                          # another PR
    'Revert "Add a helpful page"',                  # another title
    "Add a helper, again",
])
def test_other_commits_are_not_reverts(message):
    assert find_revert([CommitInfo("x" * 40, message)], MERGE_SHA, "octo/app", 7,
                       "Add a helper") is None


def test_what_counts_as_a_fix():
    assert all(looks_like_fix(m) for m in ("fix: off by one", "Fixes the helper", "hotfix",
                                           "Repair the regression", "Bug in the parser"))
    assert not any(looks_like_fix(m) for m in ("Add a prefix", "Docs\n\nfixes typos below",
                                               "Refactor"))


def test_follow_up_fixes_are_fixes_to_the_prs_files():
    commits = [
        CommitInfo("1" * 40, "fix: the helper's edge case", files=("src/app.py", "README.md")),
        CommitInfo("2" * 40, "fix: something else", files=("src/other.py",)),
        CommitInfo("3" * 40, "Add tests", files=("src/app.py",)),       # not a fix
        CommitInfo("4" * 40, "fix: never looked up"),                   # files unknown
    ]
    assert follow_up_fixes(commits, ["src/app.py", "tests/test_app.py"]) == (
        FollowUp("1" * 40, "fix: the helper's edge case", ("src/app.py",)),)


# ── the verdicts ─────────────────────────────────────────────────────────────────────────
def test_assessment_order():
    ci_ok, ci_bad = CiSummary("passing"), CiSummary("failing")
    fix = (FollowUp("1" * 40, "fix", ("a.py",)),)
    revert = CommitInfo("r" * 40, "Revert")
    assert assess(ci_bad, revert, fix, final=False) == "reverted"
    assert assess(ci_bad, None, fix, final=False) == "ci_failing"
    assert assess(ci_ok, None, fix, final=False) == "fixed_later"
    assert assess(ci_ok, None, (), final=False) == "watching"
    assert assess(CiSummary("none"), None, (), final=True) == "healthy"


@pytest.mark.parametrize(("verdict", "assessment", "call"), [
    ("merge", "healthy", "agreed"), ("merge", "reverted", "missed"),
    ("merge", "ci_failing", "missed"), ("block", "reverted", "warned"),
    ("needs_review", "fixed_later", "warned"), ("block", "healthy", "false_alarm"),
    ("needs_review", "healthy", "fine"), ("merge", "watching", "pending"),
    (None, "healthy", "not_reviewed"),
])
def test_calls(verdict, assessment, call):
    assert judge(verdict, assessment) == call


def test_when_to_look_again():
    until = MERGED + timedelta(days=7)
    assert next_look(SOON, until, "pending") == SOON + timedelta(minutes=15)
    assert next_look(SOON, until, "passing") == SOON + timedelta(days=1)
    assert next_look(until - timedelta(hours=2), until, "passing") == until
    assert next_look(until, until, "passing") is None


def test_the_comment():
    outcome = Outcome("ci_failing", CiSummary("failing", passed=("lint",), failed=("test",)),
                      follow_ups=(FollowUp("1" * 40, "fix: edge case", ("src/app.py",)),))
    body = report(outcome, repo="octo/app", merge_sha=MERGE_SHA, verdict="merge",
                  confidence=0.91, call="missed", watch_until="2026-11-15T15:57:51.000000Z",
                  checked_at="2026-11-08T16:10:00.000000Z")
    assert "❌ CI is failing on the merge commit" in body
    assert "❌ 1 failed: test" in body and "✅ 1 passed" in body
    assert "[`1111111`](https://github.com/octo/app/commit/" + "1" * 40 + ")" in body
    assert "Merge, confidence 0.91: missed this" in body
    assert "keeps watching until 2026-11-15" in body
    assert body.endswith(COMMENT_MARKER)


# ── the worker's looks ───────────────────────────────────────────────────────────────────
def decide(store, queue, verdict_outcome=None):
    """An analysis of the open PR first, as before it was merged: a merge decision."""
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    Worker(store, queue, PolicyBook(), FakeAnalyzer(verdict_outcome),
           FakeAccess(FakeGateway(make_pull()))).run_once()


def look(store, queue, gateway, at=SOON, checks=True, policies=None, **options):
    queue.enqueue("outcome", "octo/app", 7, MERGE_SHA, 11)
    Worker(store, queue, policies or PolicyBook(), FakeAnalyzer(),
           FakeAccess(gateway, checks=checks), clock=lambda: at, **options).run_once()
    return store.merge_outcome("octo/app", 7)


def queued_looks(store):
    with store.read() as conn:
        return [r["run_after"] for r in conn.execute(
            "SELECT run_after FROM jobs WHERE kind = 'outcome' AND status = 'queued'")]


def test_healthy_so_far(store, queue):
    decide(store, queue)
    gateway = FakeGateway(merged_pull(), checks=PASSING)
    row = look(store, queue, gateway)
    assert (row["assessment"], row["ci_state"], row["verdict"], row["verdict_call"],
            row["final"]) == ("watching", "passing", "merge", "pending", 0)
    assert "comment" not in gateway.names()                # nothing worth saying yet
    assert queued_looks(store) == ["2026-11-09T16:09:51.000000Z"]   # tomorrow
    assert "outcome.checked" in audit_actions(store)


def test_ci_failing_on_main_is_reported_at_once(store, queue):
    decide(store, queue)
    gateway = FakeGateway(merged_pull(), checks=FAILING)
    row = look(store, queue, gateway)
    assert (row["assessment"], row["verdict_call"]) == ("ci_failing", "missed")
    assert row["comment_id"] == 900
    assert "CI is failing" in gateway.comments[900]
    assert audit_actions(store)[-1] == "outcome.commented"


def test_a_revert(store, queue):
    revert = CommitInfo("r" * 40, f'Revert "Add a helper"\n\nThis reverts commit {MERGE_SHA}.')
    gateway = FakeGateway(merged_pull(), checks=PASSING, commits=[revert])
    row = look(store, queue, gateway)
    assert (row["assessment"], row["revert_sha"], row["verdict_call"]) == (
        "reverted", "r" * 40, "not_reviewed")
    assert "⏪ Reverted" in gateway.comments[900]


def test_follow_up_fixes(store, queue):
    commits = [CommitInfo("1" * 40, "fix: helper edge case"), CommitInfo("2" * 40, "Add docs")]
    gateway = FakeGateway(merged_pull(), checks=PASSING, commits=commits,
                          files={"1" * 40: ["src/app.py"], "2" * 40: ["src/app.py"]})
    row = look(store, queue, gateway)
    assert row["assessment"] == "fixed_later"
    assert '"sha":"' + "1" * 40 in row["follow_ups"]
    # only fix-looking commits have their files fetched
    assert [c for c in gateway.calls if c[0] == "commit_files"] == [("commit_files", "1" * 40)]


def test_after_the_watch_it_is_final(store, queue):
    decide(store, queue)
    gateway = FakeGateway(merged_pull(), checks=PASSING)
    row = look(store, queue, gateway, at=LATER)
    assert (row["assessment"], row["verdict_call"], row["final"]) == ("healthy", "agreed", 1)
    assert "✅ Held up" in gateway.comments[900] and "final" in gateway.comments[900]
    assert queued_looks(store) == []                       # the watch is over


def test_the_comment_is_updated_in_place_and_only_when_it_changes(store, queue):
    gateway = FakeGateway(merged_pull(), checks=FAILING)
    look(store, queue, gateway)
    look(store, queue, gateway)                            # still failing: nothing new to say
    assert [c for c in gateway.calls if c[0] == "comment"] == [("comment", 7, None)]
    gateway.checks = PASSING
    gateway.commits = [CommitInfo("r" * 40, "Reverts octo/app#7")]
    row = look(store, queue, gateway)
    assert row["assessment"] == "reverted"
    assert [c for c in gateway.calls if c[0] == "comment"][-1] == ("comment", 7, 900)
    assert row["looks"] == 3


def test_with_a_token_it_comments_only_when_asked(store, queue):
    gateway = FakeGateway(merged_pull(), checks=FAILING)
    look(store, queue, gateway, checks=False)
    assert "comment" not in gateway.names()
    look(store, queue, FakeGateway(merged_pull(), checks=PASSING), checks=False,
         outcome_comments=True)
    assert store.merge_outcome("octo/app", 7)["comment_id"] == 900


def test_the_policy_can_keep_quiet_or_stop_watching(store, queue):
    gateway = FakeGateway(merged_pull(), checks=FAILING)
    quiet = PolicyBook(defaults=Policy(outcome_comment=False))
    assert look(store, queue, gateway, policies=quiet)["assessment"] == "ci_failing"
    assert "comment" not in gateway.names()
    off = PolicyBook(defaults=Policy(track_outcomes=False))
    look(store, queue, FakeGateway(merged_pull()), policies=off)
    assert audit_actions(store)[-1] == "job.skipped"


def test_a_pr_closed_without_merging_is_not_watched(store, queue):
    row = look(store, queue, FakeGateway(make_pull(state="closed")))
    assert row is None and audit_actions(store)[-1] == "job.skipped"


def test_a_failed_comment_is_recorded_and_tried_again(store, queue):
    error = GithubException(403, {"message": "Resource not accessible by integration"}, None)
    gateway = FakeGateway(merged_pull(), checks=FAILING, comment_error=error)
    row = look(store, queue, gateway)
    assert row["assessment"] == "ci_failing" and row["comment_id"] is None
    assert audit_actions(store)[-1] == "outcome.comment_failed"
    gateway.comment_error = None
    assert look(store, queue, gateway)["comment_id"] == 900       # no comment yet: try again


def test_a_one_off_look_queues_no_more(store, queue):
    look(store, queue, FakeGateway(merged_pull(), checks=PASSING), keep_watching=False)
    assert queued_looks(store) == []
