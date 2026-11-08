import pytest
from conftest import NEW_SHA, SHA, audit_actions

from review_app.webhooks import (
    PayloadError,
    WebhookHandler,
    pull_from_payload,
    sign,
    verify_signature,
)

SECRET = b"s3cret"


def pr_payload(action="opened", sha=SHA, state="open", merged=False, draft=False,
               head_repo_id=1, installation=11, changes=None, repo="octo/app"):
    head_repo = {"id": head_repo_id, "full_name": "someone/app"} if head_repo_id else None
    payload = {
        "action": action,
        "number": 7,
        "pull_request": {
            "number": 7, "title": "Add a helper", "state": state, "merged": merged,
            "draft": draft, "user": {"login": "dev"},
            "head": {"sha": sha, "ref": "feature", "repo": head_repo},
            "base": {"ref": "main", "repo": {"id": 1, "full_name": repo,
                                             "default_branch": "main"}},
            "changed_files": 1, "additions": 10, "deletions": 2,
        },
        "repository": {"id": 1, "full_name": repo, "default_branch": "main"},
        "installation": {"id": installation},
    }
    if changes is not None:
        payload["changes"] = changes
    return payload


@pytest.fixture
def handler(store, queue) -> WebhookHandler:
    return WebhookHandler(store, queue, app_id=42)


def jobs(store) -> list[tuple]:
    with store.read() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT kind, repo, pr_number, head_sha, installation_id, status FROM jobs ORDER BY id")]


# ── signatures ───────────────────────────────────────────────────────────────────────────
def test_a_valid_signature_checks_out():
    body = b'{"zen": "Design for failure."}'
    assert verify_signature(SECRET, body, sign(SECRET, body))


@pytest.mark.parametrize(("secret", "body", "header"), [
    (SECRET, b"{}", sign(SECRET, b"{} ")),                  # body changed
    (SECRET, b"{}", sign(b"other", b"{}")),                 # another secret
    (SECRET, b"{}", None),                                  # unsigned
    (SECRET, b"{}", "sha1=" + sign(SECRET, b"{}")[7:]),     # the old SHA-1 header
    (SECRET, b"{}", "sha256=ünïcode"),                      # must not raise
    (b"", b"{}", sign(b"", b"{}")),                         # no secret: trust nothing
])
def test_anything_else_is_refused(secret, body, header):
    assert not verify_signature(secret, body, header)


# ── pull requests ────────────────────────────────────────────────────────────────────────
def test_an_opened_pr_queues_an_analysis(handler, store):
    result = handler.handle("pull_request", "d-1", pr_payload())

    assert (result.status, result.message) == (202, "analysis queued")
    assert jobs(store) == [("analyze", "octo/app", 7, SHA, 11, "queued")]
    with store.read() as conn:
        repo = conn.execute("SELECT * FROM repositories").fetchone()
        pr = conn.execute("SELECT * FROM pull_requests").fetchone()
    assert (repo["full_name"], repo["installation_id"], repo["default_branch"]) == (
        "octo/app", 11, "main")
    assert (pr["number"], pr["head_sha"], pr["state"], pr["is_fork"]) == (7, SHA, "open", 0)
    assert audit_actions(store) == ["job.queued"]


@pytest.mark.parametrize("action", ["reopened", "synchronize", "ready_for_review"])
def test_other_changes_to_the_code_queue_an_analysis(handler, store, action):
    assert handler.handle("pull_request", "d-1", pr_payload(action)).job_id is not None


def test_a_push_supersedes_the_queued_analysis(handler, store):
    handler.handle("pull_request", "d-1", pr_payload())
    handler.handle("pull_request", "d-2", pr_payload("synchronize", sha=NEW_SHA))
    assert [(j[3], j[5]) for j in jobs(store)] == [(SHA, "superseded"), (NEW_SHA, "queued")]


def test_a_redelivery_is_handled_once(handler, store):
    handler.handle("pull_request", "d-1", pr_payload())
    again = handler.handle("pull_request", "d-1", pr_payload())
    assert (again.status, again.message) == (200, "already handled this delivery")
    assert len(jobs(store)) == 1


def test_a_base_branch_change_queues_an_analysis(handler, store):
    assert handler.handle("pull_request", "d-1", pr_payload(
        "edited", changes={"title": {"from": "x"}})).job_id is None
    assert handler.handle("pull_request", "d-2", pr_payload(
        "edited", changes={"base": {"ref": {"from": "dev"}}})).job_id is not None


@pytest.mark.parametrize("action", ["labeled", "assigned", "review_requested"])
def test_other_actions_change_nothing(handler, store, action):
    result = handler.handle("pull_request", "d-1", pr_payload(action))
    assert result.job_id is None
    assert jobs(store) == []


def test_closing_a_pr_cancels_its_jobs(handler, store):
    handler.handle("pull_request", "d-1", pr_payload())
    handler.handle("pull_request", "d-2", pr_payload("closed", state="closed", merged=True))

    assert jobs(store)[0][5] == "cancelled"
    with store.read() as conn:
        assert conn.execute("SELECT state FROM pull_requests").fetchone()[0] == "merged"
    assert audit_actions(store) == ["job.queued", "pr.closed"]


def test_a_fork_pr_is_analysed_but_marked(handler, store):
    handler.handle("pull_request", "d-1", pr_payload(head_repo_id=99))
    with store.read() as conn:
        assert conn.execute("SELECT is_fork FROM pull_requests").fetchone()[0] == 1
    assert len(jobs(store)) == 1


def test_a_deleted_fork_counts_as_a_fork():
    assert pull_from_payload(pr_payload(head_repo_id=None)).is_fork


@pytest.mark.parametrize("payload", [
    {"action": "opened"},
    {**pr_payload(), "repository": {"full_name": "not a repo"}},
    {**pr_payload(), "pull_request": {**pr_payload()["pull_request"], "number": "7"}},
])
def test_a_malformed_payload_is_refused_and_can_be_redelivered(handler, store, payload):
    with pytest.raises(PayloadError):
        handler.handle("pull_request", "d-1", payload)
    # the delivery wasn't recorded: a fixed redelivery still counts
    assert handler.handle("pull_request", "d-1", pr_payload()).job_id is not None


# ── installations ────────────────────────────────────────────────────────────────────────
def test_installations_keep_the_repository_list(handler, store):
    handler.handle("installation", "d-1", {
        "action": "created", "installation": {"id": 11, "account": {"login": "octo"}},
        "repositories": [{"full_name": "octo/app"}, {"full_name": "octo/lib"}]})
    handler.handle("installation_repositories", "d-2", {
        "action": "removed", "installation": {"id": 11},
        "repositories_added": [{"full_name": "octo/new"}],
        "repositories_removed": [{"full_name": "octo/lib"}]})
    with store.read() as conn:
        active = dict(conn.execute("SELECT full_name, active FROM repositories").fetchall())
    assert active == {"octo/app": 1, "octo/lib": 0, "octo/new": 1}

    handler.handle("installation", "d-3", {"action": "deleted", "installation": {"id": 11}})
    with store.read() as conn:
        assert conn.execute("SELECT MAX(active) FROM repositories").fetchone()[0] == 0
    assert audit_actions(store) == ["installation.created",
                                    "installation.repositories_changed", "installation.deleted"]


# ── checks finishing ─────────────────────────────────────────────────────────────────────
def waiting_merge(store):
    from conftest import make_pull
    with store.transaction() as conn:
        store.upsert_repo(conn, "octo/app", 11, "main")
        pr_id = store.upsert_pr(conn, make_pull())
        decision = store.add_decision(conn, pr_id, None, SHA, "merge", 1.0, True, "auto", [], {})
    store.set_outcome(decision, "merge_waiting", merge_attempted=True)


def suite(conclusion="success", app_id=7, action="completed"):
    return {"action": action, "repository": {"full_name": "octo/app"},
            "installation": {"id": 11},
            "check_suite": {"head_sha": SHA, "conclusion": conclusion, "app": {"id": app_id}}}


def test_passing_checks_retry_a_waiting_merge(handler, store):
    waiting_merge(store)
    result = handler.handle("check_suite", "d-1", suite())
    assert result.job_id is not None
    assert jobs(store) == [("merge", "octo/app", 7, SHA, 11, "queued")]


@pytest.mark.parametrize("payload", [
    suite(conclusion="failure"), suite(action="requested"), suite(app_id=42),   # 42: ourselves
])
def test_other_check_suites_change_nothing(handler, store, payload):
    waiting_merge(store)
    handler.handle("check_suite", "d-1", payload)
    assert jobs(store) == []


def test_a_successful_status_retries_a_waiting_merge(handler, store):
    waiting_merge(store)
    status = {"state": "success", "sha": SHA, "repository": {"full_name": "octo/app"}}
    assert handler.handle("status", "d-1", status).job_id is not None
    assert handler.handle("status", "d-2", {**status, "state": "pending"}).job_id is None


def test_ping_and_unknown_events(handler):
    assert handler.handle("ping", "d-1", {"zen": "hi"}).message == "pong"
    assert handler.handle("star", "d-2", {}).status == 202
