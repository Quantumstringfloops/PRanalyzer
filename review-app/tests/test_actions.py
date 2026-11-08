"""The executor: what is done about a decision, and the gates checked again right before a merge."""
import pytest
from conftest import NEW_SHA, SHA, FakeGateway, audit_actions, make_pull
from github import GithubException

from review_app.actions import Executor, check_run_summary, merge_gate_problems
from review_app.decision import Decision, Verdict
from review_app.models import MergeResult
from review_app.policy import Policy

AUTO = Policy(mode="auto")
RECOMMEND = Policy()


def decision(verdict: Verdict = Verdict.MERGE, *reasons: str) -> Decision:
    return Decision(verdict, 0.95, True, reasons or ("all clear",), {"score": 95.0, "grade": "A"})


def setup_decision(store, verdict: Verdict = Verdict.MERGE, mode: str = "auto") -> int:
    with store.transaction() as conn:
        pr_id = store.upsert_pr(conn, make_pull())
        return store.add_decision(conn, pr_id, None, SHA, verdict.value, 0.95, True, mode,
                                  [], {})


def outcome_of(store, decision_id: int) -> tuple[str, int]:
    with store.read() as conn:
        row = conn.execute("SELECT outcome, merge_attempts FROM decisions WHERE id = ?",
                           (decision_id,)).fetchone()
    return row["outcome"], row["merge_attempts"]


def carry_out(store, gateway, verdict=Verdict.MERGE, policy=AUTO, checks=True, pull=None,
              sha=SHA, reasons=()) -> tuple[str, int]:
    decision_id = setup_decision(store, verdict, policy.mode)
    outcome = Executor(store).carry_out(
        gateway, checks=checks, decision_id=decision_id, decision=decision(verdict, *reasons),
        pull=pull or make_pull(), policy=policy, analysed_sha=sha)
    return outcome, decision_id


def test_recommend_mode_merges_nothing(store):
    gateway = FakeGateway()
    outcome, decision_id = carry_out(store, gateway, policy=RECOMMEND)

    assert outcome == "merge_recommended"
    assert "merge" not in gateway.names()
    assert gateway.calls[0][2:4] == ("success", "Merge recommended (recommend-only mode)")
    assert outcome_of(store, decision_id) == ("merge_recommended", 0)
    assert audit_actions(store) == ["check_run.created", "merge.recommended"]


def test_auto_mode_merges_through_the_api_after_the_check_run(store):
    gateway = FakeGateway()
    outcome, decision_id = carry_out(store, gateway)

    assert outcome == "merged"
    assert gateway.names() == ["check_run", "pull", "merge"]   # the check run first
    assert gateway.calls[2] == ("merge", "octo/app", 7, SHA, "squash", "Add a helper (#7)")
    assert outcome_of(store, decision_id) == ("merged", 1)
    assert audit_actions(store) == ["check_run.created", "merge.attempted", "merge.succeeded"]


@pytest.mark.parametrize(("fresh", "problem"), [
    (make_pull(head_sha=NEW_SHA), "new commits arrived after the analysis"),
    (make_pull(head_repo_id=99), "the PR comes from a fork"),
    (make_pull(head_repo_id=None), "the PR comes from a fork"),
    (make_pull(is_draft=True), "the PR is a draft"),
    (make_pull(state="closed"), "the PR is closed"),
    (make_pull(state="merged"), "the PR is merged"),
    (make_pull(base_branch="release"), "the PR targets release"),
])
def test_a_merge_is_called_off_when_the_pr_changed(store, fresh, problem):
    gateway = FakeGateway(fresh)
    outcome, decision_id = carry_out(store, gateway)

    assert outcome == "merge_aborted"
    assert "merge" not in gateway.names()
    assert outcome_of(store, decision_id) == ("merge_aborted", 0)
    with store.read() as conn:
        details = conn.execute("SELECT details FROM audit_log WHERE action = 'merge.aborted'"
                               ).fetchone()[0]
    assert problem in details


def test_a_merge_held_for_checks_waits(store):
    gateway = FakeGateway(merge=MergeResult(False, 'Required status check "ci" is expected.',
                                            checks_pending=True))
    outcome, decision_id = carry_out(store, gateway)
    assert outcome == "merge_waiting"
    assert outcome_of(store, decision_id) == ("merge_waiting", 1)
    assert audit_actions(store)[-1] == "merge.waiting"


def test_a_refused_merge_is_recorded_not_worked_around(store):
    gateway = FakeGateway(merge=MergeResult(False, "At least 1 approving review is required"))
    outcome, _ = carry_out(store, gateway)
    assert outcome == "merge_refused"
    assert gateway.names().count("merge") == 1
    assert audit_actions(store)[-1] == "merge.refused"


def test_a_merge_that_errors_is_left_for_a_person(store):
    gateway = FakeGateway(merge=GithubException(502, {"message": "Bad gateway"}, None))
    outcome, decision_id = carry_out(store, gateway)
    assert outcome == "merge_failed"
    assert gateway.names().count("merge") == 1     # never sent twice
    assert outcome_of(store, decision_id) == ("merge_failed", 1)


def test_a_pr_that_cannot_be_read_again_is_not_merged(store):
    gateway = FakeGateway(pull_error=GithubException(500, {"message": "boom"}, None))
    outcome, _ = carry_out(store, gateway)
    assert outcome == "merge_failed"
    assert "merge" not in gateway.names()


def test_a_failed_check_run_does_not_stop_the_decision(store):
    gateway = FakeGateway(check_run_error=GithubException(403, {"message": "no"}, None))
    outcome, _ = carry_out(store, gateway, verdict=Verdict.BLOCK)
    assert outcome == "blocked"
    assert audit_actions(store) == ["check_run.failed"]


def test_no_check_run_without_a_github_app(store):
    gateway = FakeGateway()
    carry_out(store, gateway, policy=RECOMMEND, checks=False)
    assert "check_run" not in gateway.names()


def test_a_block_fails_the_check_and_merges_nothing(store):
    gateway = FakeGateway()
    outcome, _ = carry_out(store, gateway, verdict=Verdict.BLOCK)
    assert outcome == "blocked"
    assert gateway.names() == ["check_run"]
    assert gateway.calls[0][2:4] == ("failure", "Blocked")


def test_needs_review_requests_reviewers_in_auto_mode(store):
    policy = Policy(mode="auto", reviewers=("alice", "Dev"), team_reviewers=("core",))
    gateway = FakeGateway()
    outcome, _ = carry_out(store, gateway, verdict=Verdict.NEEDS_REVIEW, policy=policy)

    assert outcome == "review_requested"
    assert gateway.calls[0][2:4] == ("neutral", "Needs human review")
    assert gateway.calls[1] == ("reviewers", ["alice"], ["core"])   # never the PR's author


def test_needs_review_requests_nobody_in_recommend_mode(store):
    gateway = FakeGateway()
    outcome, _ = carry_out(store, gateway, verdict=Verdict.NEEDS_REVIEW,
                           policy=Policy(reviewers=("alice",)))
    assert outcome == "needs_review"
    assert "reviewers" not in gateway.names()


def test_a_failed_review_request_is_recorded(store):
    gateway = FakeGateway(reviewers_error=GithubException(422, {"message": "not a collaborator"},
                                                          None))
    outcome, _ = carry_out(store, gateway, verdict=Verdict.NEEDS_REVIEW,
                           policy=Policy(mode="auto", reviewers=("alice",)))
    assert outcome == "review_request_failed"


def test_the_check_run_summary_neutralises_pr_text():
    summary = check_run_summary(
        decision(Verdict.NEEDS_REVIEW, "It changes paths: @octocat [x](https://evil.example)"),
        "recommend")
    assert "@octocat" not in summary
    assert "](https://evil.example)" not in summary
    assert "recommend-only: the app merges nothing" in summary
    assert "**Score:** 95.0 (A)" in summary


def test_merge_gates_hold_for_a_pr_that_may_merge():
    assert merge_gate_problems(make_pull(), SHA, AUTO) == []
    assert merge_gate_problems(make_pull(), SHA, RECOMMEND) == [
        "auto-merge is off for this repository"]
