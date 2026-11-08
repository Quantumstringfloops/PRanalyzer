import sqlite3

import pytest
from conftest import make_pull

from review_app.db import GENESIS_HASH, Store


def test_init_is_idempotent(store):
    store.init()
    store.init()
    with store.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_an_unknown_schema_version_is_refused(store):
    with store.read() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version 99"):
        store.init()


def test_the_audit_log_is_a_hash_chain(store):
    store.audit("worker", "decision.made", "octo/app", 7, "a" * 40, verdict="merge")
    store.audit("worker", "merge.succeeded", "octo/app", 7, "a" * 40)
    with store.read() as conn:
        first, second = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    assert first["prev_hash"] == GENESIS_HASH
    assert second["prev_hash"] == first["hash"]
    assert store.verify_audit_chain() == (True, None, 2)


def test_the_audit_log_refuses_updates_and_deletes(store):
    store.audit("worker", "decision.made", verdict="merge")
    with store.read() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE audit_log SET action = 'decision.hidden'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM audit_log")


@pytest.mark.parametrize("tamper", [
    "UPDATE audit_log SET details = '{\"verdict\":\"merge\"}' WHERE id = 2",   # was block
    "DELETE FROM audit_log WHERE id = 2",
    "UPDATE audit_log SET at = '2000-01-01T00:00:00.000000Z' WHERE id = 1",
])
def test_edits_made_around_the_app_break_the_chain(store, tamper):
    for verdict in ("merge", "block", "needs_review"):
        store.audit("worker", "decision.made", verdict=verdict)
    with store.read() as conn:   # someone with the database file drops the guards
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("DROP TRIGGER audit_log_no_delete")
        conn.execute(tamper)
    intact, bad_row, _ = store.verify_audit_chain()
    assert not intact
    assert bad_row in (1, 2, 3)


def test_deliveries_are_recorded_once(store):
    assert store.first_delivery("guid-1", "pull_request")
    assert not store.first_delivery("guid-1", "pull_request")


def test_a_read_only_store_cannot_write(store, tmp_path):
    store.audit("worker", "decision.made")
    reader = Store(store.path, readonly=True)
    with reader.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO deliveries VALUES ('x', 'ping', 'now')")


def test_repositories_keep_their_state_unless_told(store):
    with store.transaction() as conn:
        store.upsert_repo(conn, "octo/app", 11, "main", active=False)
        store.upsert_repo(conn, "Octo/App", None, None, active=None)   # names ignore case
        row = conn.execute("SELECT * FROM repositories").fetchone()
    assert (row["installation_id"], row["default_branch"], row["active"]) == (11, "main", 0)


def test_pull_requests_are_upserted(store):
    with store.transaction() as conn:
        first = store.upsert_pr(conn, make_pull(title="One"))
        again = store.upsert_pr(conn, make_pull(title="Two", head_sha="b" * 40))
        row = conn.execute("SELECT * FROM pull_requests").fetchone()
    assert first == again
    assert (row["title"], row["head_sha"]) == ("Two", "b" * 40)


def test_waiting_merges_only_finds_the_latest_decision(store):
    with store.transaction() as conn:
        pr_id = store.upsert_pr(conn, make_pull())
        old = store.add_decision(conn, pr_id, None, "a" * 40, "merge", 1.0, True, "auto", [], {})
    store.set_outcome(old, "merge_waiting", merge_attempted=True)
    assert [r["number"] for r in store.waiting_merges("octo/app", "a" * 40)] == [7]
    with store.transaction() as conn:
        store.add_decision(conn, pr_id, None, "a" * 40, "needs_review", 0.5, True, "auto", [], {})
    assert store.waiting_merges("octo/app", "a" * 40) == []
