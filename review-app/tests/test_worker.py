"""The worker, end to end: a queued job through analyze → decide → act, with fakes for GitHub
and pr-analyzer."""
import json
from datetime import datetime, timedelta

import pytest
from conftest import (
    NEW_SHA,
    SHA,
    FakeAccess,
    FakeAnalyzer,
    FakeGateway,
    audit_actions,
    make_outcome,
    make_pull,
    make_result,
)
from github import GithubException
from pr_analyzer.exceptions import PRAnalyzerError, RateLimitError

from review_app.models import MergeResult
from review_app.policy import Policy, PolicyBook
from review_app.worker import Worker, retry_delay


def make_worker(store, queue, gateway=None, analyzer=None, policy=None, checks=True):
    gateway = gateway or FakeGateway()
    policies = PolicyBook(repos={"octo/app": policy or Policy(mode="auto")})
    worker = Worker(store, queue, policies, analyzer or FakeAnalyzer(),
                    FakeAccess(gateway, checks=checks))
    return worker, gateway


def latest(store) -> dict:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    return {**dict(row), "reasons": json.loads(row["reasons"])} if row else {}


def job_row(store) -> dict:
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone())


def test_a_clean_pr_is_analysed_decided_and_merged(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    analyzer = FakeAnalyzer()
    worker, gateway = make_worker(store, queue, analyzer=analyzer)

    assert worker.run_once()

    assert analyzer.calls[0]["token"] == "ghs_test"
    assert analyzer.calls[0]["post_review"] is True
    decision = latest(store)
    assert (decision["verdict"], decision["outcome"], decision["mode"]) == ("merge", "merged",
                                                                            "auto")
    assert job_row(store)["status"] == "done"
    assert audit_actions(store) == ["analysis.completed", "decision.made", "check_run.created",
                                    "merge.attempted", "merge.succeeded"]
    assert gateway.closed
    with store.read() as conn:
        analysis = conn.execute("SELECT * FROM analyses").fetchone()
    assert (analysis["score"], analysis["grade"], analysis["llm_provider"]) == (95.0, "A",
                                                                                "nvidia")


def test_recommend_mode_only_recommends(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    worker, gateway = make_worker(store, queue, policy=Policy())
    worker.run_once()
    assert latest(store)["outcome"] == "merge_recommended"
    assert "merge" not in gateway.names()


def test_an_incomplete_analysis_is_sent_to_a_human(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    outcome = make_outcome(make_result(warnings=["Semgrep scan skipped: semgrep is not installed"]))
    worker, gateway = make_worker(store, queue, analyzer=FakeAnalyzer(outcome))
    worker.run_once()
    decision = latest(store)
    assert (decision["verdict"], decision["complete"]) == ("needs_review", 0)
    assert "merge" not in gateway.names()


def test_a_failed_analysis_still_ends_in_a_decision(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    analyzer = FakeAnalyzer(error=PRAnalyzerError("Your token is not allowed to access octo/app"))
    worker, gateway = make_worker(store, queue, analyzer=analyzer)
    worker.run_once()

    decision = latest(store)
    assert decision["verdict"] == "needs_review"
    assert decision["reasons"][0] == ("The analysis didn't finish: Your token is not allowed to "
                                      "access octo/app")
    assert gateway.calls[1][2] == "neutral"
    assert job_row(store)["status"] == "done"
    assert "analysis.failed" in audit_actions(store)


def test_a_rate_limit_puts_the_job_back(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    reset = datetime.now() + timedelta(minutes=10)
    worker, _ = make_worker(store, queue, analyzer=FakeAnalyzer(error=RateLimitError(reset)))
    worker.run_once()

    row = job_row(store)
    assert (row["status"], row["attempts"]) == ("queued", 1)
    assert latest(store) == {}
    assert audit_actions(store) == ["job.retrying"]


def test_a_passing_error_on_the_last_try_ends_in_a_decision(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    worker, _ = make_worker(store, queue, analyzer=FakeAnalyzer(
        error=PRAnalyzerError("Could not reach GitHub: connection reset")))
    with store.transaction() as conn:   # two tries already used
        conn.execute("UPDATE jobs SET attempts = 2")
    worker.run_once()
    decision = latest(store)
    assert decision["verdict"] == "needs_review"
    assert "gave up after 3 tries" in decision["reasons"][0]


def test_a_closed_pr_is_skipped(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    analyzer = FakeAnalyzer()
    worker, _ = make_worker(store, queue, gateway=FakeGateway(make_pull(state="closed")),
                            analyzer=analyzer)
    worker.run_once()
    assert analyzer.calls == []
    assert audit_actions(store) == ["job.skipped"]


def test_a_job_for_an_old_head_is_superseded(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    analyzer = FakeAnalyzer()
    worker, _ = make_worker(store, queue, gateway=FakeGateway(make_pull(head_sha=NEW_SHA)),
                            analyzer=analyzer)
    worker.run_once()

    assert analyzer.calls == []
    with store.read() as conn:
        rows = conn.execute("SELECT head_sha, status FROM jobs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(SHA, "superseded"), (NEW_SHA, "queued")]


def test_a_repository_switched_off_is_left_alone(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    analyzer = FakeAnalyzer()
    worker, gateway = make_worker(store, queue, analyzer=analyzer, policy=Policy(enabled=False))
    worker.run_once()
    assert analyzer.calls == [] and gateway.calls == []


def test_new_commits_during_the_analysis_stop_the_merge(store, queue):
    # pr-analyzer analysed SHA; by merge time the PR is at NEW_SHA
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    gateway = FakeGateway(make_pull(), make_pull(head_sha=NEW_SHA))
    worker, _ = make_worker(store, queue, gateway=gateway)
    worker.run_once()
    assert latest(store)["outcome"] == "merge_aborted"
    assert "merge" not in gateway.names()


def test_a_crashing_job_fails_without_stopping_the_worker(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    worker, _ = make_worker(store, queue, gateway=FakeGateway(pull_error=ValueError("bug")))
    worker.run_once()
    assert job_row(store)["status"] == "failed"
    assert audit_actions(store) == ["job.failed"]
    assert worker.run_once() is False


def test_github_trouble_reading_the_pr_is_retried(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    gateway = FakeGateway(pull_error=GithubException(502, {"message": "Bad gateway"}, None))
    worker, _ = make_worker(store, queue, gateway=gateway)
    worker.run_once()
    assert job_row(store)["status"] == "queued"


def waiting(store, queue, attempts=1):
    with store.transaction() as conn:
        pr_id = store.upsert_pr(conn, make_pull())
        decision = store.add_decision(conn, pr_id, None, SHA, "merge", 1.0, True, "auto", [], {})
        conn.execute("UPDATE decisions SET outcome = 'merge_waiting', merge_attempts = ?",
                     (attempts,))
    queue.enqueue("merge", "octo/app", 7, SHA, 11)
    return decision


def test_a_merge_waiting_for_checks_is_tried_again(store, queue):
    waiting(store, queue)
    worker, gateway = make_worker(store, queue)
    worker.run_once()
    assert gateway.names() == ["pull", "merge"]
    assert (latest(store)["outcome"], latest(store)["merge_attempts"]) == ("merged", 2)


def test_a_merge_still_held_keeps_waiting(store, queue):
    waiting(store, queue)
    worker, _ = make_worker(store, queue, gateway=FakeGateway(
        merge=MergeResult(False, "Required status check is expected", checks_pending=True)))
    worker.run_once()
    assert latest(store)["outcome"] == "merge_waiting"


def test_waiting_for_checks_gives_up_after_five_tries(store, queue):
    waiting(store, queue, attempts=5)
    worker, gateway = make_worker(store, queue)
    worker.run_once()
    assert gateway.calls == []
    assert latest(store)["outcome"] == "merge_refused"
    assert audit_actions(store) == ["merge.gave_up"]


def test_a_merge_retry_needs_a_waiting_merge_decision(store, queue):
    queue.enqueue("merge", "octo/app", 7, SHA, 11)
    worker, gateway = make_worker(store, queue)
    worker.run_once()
    assert gateway.calls == []
    assert audit_actions(store) == ["merge.retry_skipped"]


@pytest.mark.parametrize(("error", "retried"), [
    (RateLimitError(datetime.now() + timedelta(minutes=5)), True),
    (GithubException(503, {"message": "unavailable"}, None), True),
    (PRAnalyzerError("Could not reach GitHub: timeout"), True),
    (PRAnalyzerError("GitHub error 502: bad gateway"), True),
    (PRAnalyzerError("GitHub error 422: invalid"), False),
    (GithubException(404, {"message": "Not Found"}, None), False),
    (ValueError("bug"), False),
])
def test_which_errors_are_worth_retrying(error, retried):
    assert (retry_delay(error, 1) is not None) is retried
