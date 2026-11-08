"""SQLite storage: repositories, pull requests, analyses, decisions, jobs and the audit log.

Every unit of work opens its own short-lived connection, so the webhook server's threads, the
worker and the dashboard never share one. WAL mode lets the dashboard read while the worker
writes. The audit log is append-only (triggers refuse UPDATE and DELETE), and each row carries
the SHA-256 of the previous row's hash and its own content, so verify_audit_chain() can tell
when rows were edited, removed or reordered behind the app's back."""
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_TEXT = 2000   # longest error or message kept in a detail field

SCHEMA = """
CREATE TABLE repositories (
    full_name       TEXT PRIMARY KEY COLLATE NOCASE,
    installation_id INTEGER,
    default_branch  TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);
CREATE TABLE pull_requests (
    id          INTEGER PRIMARY KEY,
    repo        TEXT NOT NULL COLLATE NOCASE,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    head_sha    TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    is_fork     INTEGER NOT NULL DEFAULT 0,
    is_draft    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'open',
    updated_at  TEXT NOT NULL,
    UNIQUE (repo, number)
);
CREATE TABLE analyses (
    id             INTEGER PRIMARY KEY,
    pr_id          INTEGER NOT NULL REFERENCES pull_requests(id),
    head_sha       TEXT NOT NULL,
    score          REAL,
    grade          TEXT,
    passed         INTEGER,
    critical_count INTEGER,
    high_count     INTEGER,
    dimensions     TEXT NOT NULL DEFAULT '{}',
    findings       TEXT NOT NULL DEFAULT '[]',
    warnings       TEXT NOT NULL DEFAULT '[]',
    llm_provider   TEXT,
    duration       REAL,
    error          TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE decisions (
    id             INTEGER PRIMARY KEY,
    pr_id          INTEGER NOT NULL REFERENCES pull_requests(id),
    analysis_id    INTEGER REFERENCES analyses(id),
    head_sha       TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('merge', 'needs_review', 'block')),
    confidence     REAL NOT NULL,
    complete       INTEGER NOT NULL,
    mode           TEXT NOT NULL,
    reasons        TEXT NOT NULL,
    facts          TEXT NOT NULL,
    outcome        TEXT,
    merge_attempts INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX decisions_by_pr ON decisions (pr_id, id);
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('analyze', 'merge')),
    repo            TEXT NOT NULL COLLATE NOCASE,
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT NOT NULL DEFAULT '',
    installation_id INTEGER,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    run_after       TEXT NOT NULL,
    locked_until    TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX jobs_ready ON jobs (status, run_after);
CREATE INDEX jobs_by_pr ON jobs (repo, pr_number, status);
CREATE TABLE deliveries (
    id          TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    repo      TEXT,
    pr_number INTEGER,
    head_sha  TEXT,
    details   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE INDEX audit_by_pr ON audit_log (repo, pr_number);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime | None = None) -> str:
    """A UTC time as text whose order is its time order, so SQL can compare them as strings."""
    return (moment or utcnow()).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def clip(text: object, limit: int = MAX_TEXT) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def to_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def audit_hash(prev_hash: str, row: tuple[Any, ...]) -> str:
    return hashlib.sha256(to_json([prev_hash, *row]).encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly

    # ── connections ─────────────────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        if self.readonly:
            # mode=ro: the dashboard can't write even if a bug asked it to
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                   timeout=10, isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE takes the write lock at once, so read-then-write steps can't interleave."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init(self) -> None:
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};"
                                   "COMMIT;")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema version {version}; this build "
                                   f"understands {SCHEMA_VERSION}")
        finally:
            conn.close()

    # ── audit log ───────────────────────────────────────────────────────────────────────
    def audit(self, actor: str, action: str, repo: str | None = None,
              pr_number: int | None = None, head_sha: str | None = None,
              conn: sqlite3.Connection | None = None, **details: Any) -> None:
        if conn is None:
            with self.transaction() as own:
                self._append_audit(own, actor, action, repo, pr_number, head_sha, details)
        else:
            self._append_audit(conn, actor, action, repo, pr_number, head_sha, details)

    @staticmethod
    def _append_audit(conn: sqlite3.Connection, actor: str, action: str, repo: str | None,
                      pr_number: int | None, head_sha: str | None,
                      details: dict[str, Any]) -> None:
        last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS_HASH
        row = (stamp(), actor, action, repo, pr_number, head_sha or None, to_json(details))
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, repo, pr_number, head_sha, details,"
            " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*row, prev_hash, audit_hash(prev_hash, row)),
        )

    def verify_audit_chain(self) -> tuple[bool, int | None, int]:
        """(intact, id of the first bad row, rows checked)."""
        prev_hash = GENESIS_HASH
        checked = 0
        with self.read() as conn:
            for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
                row = (r["at"], r["actor"], r["action"], r["repo"], r["pr_number"], r["head_sha"],
                       r["details"])
                if r["prev_hash"] != prev_hash or r["hash"] != audit_hash(prev_hash, row):
                    return False, r["id"], checked
                prev_hash = r["hash"]
                checked += 1
        return True, None, checked

    # ── webhook deliveries ──────────────────────────────────────────────────────────────
    def first_delivery(self, delivery_id: str, event: str) -> bool:
        """Record a delivery; False if it was seen before (GitHub redelivers on timeouts)."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO deliveries (id, event, received_at) VALUES (?, ?, ?)",
                (delivery_id, event, stamp()),
            )
            return cursor.rowcount == 1

    # ── repositories and pull requests ──────────────────────────────────────────────────
    def upsert_repo(self, conn: sqlite3.Connection, full_name: str,
                    installation_id: int | None = None, default_branch: str | None = None,
                    active: bool | None = True) -> None:
        """Insert or refresh a repository; None leaves what's known (and a new row active)."""
        conn.execute(
            "INSERT INTO repositories (full_name, installation_id, default_branch, active,"
            " updated_at) VALUES (?, ?, ?, COALESCE(?, 1), ?) ON CONFLICT (full_name) DO UPDATE"
            " SET installation_id = COALESCE(excluded.installation_id, installation_id),"
            " default_branch = COALESCE(excluded.default_branch, default_branch),"
            " active = COALESCE(?, active), updated_at = excluded.updated_at",
            (full_name, installation_id, default_branch, None if active is None else int(active),
             stamp(), None if active is None else int(active)),
        )

    def deactivate_installation(self, conn: sqlite3.Connection, installation_id: int) -> int:
        cursor = conn.execute(
            "UPDATE repositories SET active = 0, updated_at = ? WHERE installation_id = ?",
            (stamp(), installation_id),
        )
        return cursor.rowcount

    def upsert_pr(self, conn: sqlite3.Connection, pull: Any) -> int:
        """Insert or refresh a PullInfo; returns the row id."""
        conn.execute(
            "INSERT INTO pull_requests (repo, number, title, author, head_sha, base_branch,"
            " is_fork, is_draft, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (repo, number) DO UPDATE SET title = excluded.title,"
            " author = excluded.author, head_sha = excluded.head_sha,"
            " base_branch = excluded.base_branch, is_fork = excluded.is_fork,"
            " is_draft = excluded.is_draft, state = excluded.state,"
            " updated_at = excluded.updated_at",
            (pull.repo, pull.number, clip(pull.title, 300), pull.author, pull.head_sha,
             pull.base_branch, int(pull.is_fork), int(pull.is_draft), pull.state, stamp()),
        )
        return self.pr_id(conn, pull.repo, pull.number)

    @staticmethod
    def pr_id(conn: sqlite3.Connection, repo: str, number: int) -> int:
        row = conn.execute("SELECT id FROM pull_requests WHERE repo = ? AND number = ?",
                           (repo, number)).fetchone()
        return int(row["id"])

    def set_pr_state(self, conn: sqlite3.Connection, repo: str, number: int, state: str) -> None:
        conn.execute("UPDATE pull_requests SET state = ?, updated_at = ? WHERE repo = ? AND"
                     " number = ?", (state, stamp(), repo, number))

    # ── analyses and decisions ──────────────────────────────────────────────────────────
    def add_analysis(self, conn: sqlite3.Connection, pr_id: int, head_sha: str,
                     summary: dict[str, Any]) -> int:
        cursor = conn.execute(
            "INSERT INTO analyses (pr_id, head_sha, score, grade, passed, critical_count,"
            " high_count, dimensions, findings, warnings, llm_provider, duration, error,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pr_id, head_sha, summary.get("score"), summary.get("grade"),
             None if summary.get("passed") is None else int(summary["passed"]),
             summary.get("critical_count"), summary.get("high_count"),
             to_json(summary.get("dimensions", {})), to_json(summary.get("findings", [])),
             to_json(summary.get("warnings", [])), summary.get("llm_provider"),
             summary.get("duration"), summary.get("error"), stamp()),
        )
        return int(cursor.lastrowid or 0)

    def add_decision(self, conn: sqlite3.Connection, pr_id: int, analysis_id: int | None,
                     head_sha: str, verdict: str, confidence: float, complete: bool, mode: str,
                     reasons: list[str], facts: dict[str, Any]) -> int:
        now = stamp()
        cursor = conn.execute(
            "INSERT INTO decisions (pr_id, analysis_id, head_sha, verdict, confidence, complete,"
            " mode, reasons, facts, created_at, updated_at) VALUES"
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pr_id, analysis_id, head_sha, verdict, confidence, int(complete), mode,
             to_json(reasons), to_json(facts), now, now),
        )
        return int(cursor.lastrowid or 0)

    def set_outcome(self, decision_id: int, outcome: str, merge_attempted: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE decisions SET outcome = ?, updated_at = ?,"
                " merge_attempts = merge_attempts + ? WHERE id = ?",
                (outcome, stamp(), int(merge_attempted), decision_id),
            )

    def latest_decision(self, repo: str, number: int) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(
                "SELECT d.* FROM decisions d JOIN pull_requests p ON p.id = d.pr_id"
                " WHERE p.repo = ? AND p.number = ? ORDER BY d.id DESC LIMIT 1",
                (repo, number),
            ).fetchone()

    def waiting_merges(self, repo: str, head_sha: str) -> list[sqlite3.Row]:
        """PRs whose latest decision is a merge GitHub held back until checks pass, at head_sha."""
        with self.read() as conn:
            return conn.execute(
                "SELECT p.repo, p.number, d.head_sha FROM decisions d"
                " JOIN pull_requests p ON p.id = d.pr_id"
                " WHERE p.repo = ? AND d.head_sha = ? AND d.outcome = 'merge_waiting'"
                " AND p.state = 'open'"
                " AND d.id = (SELECT MAX(id) FROM decisions WHERE pr_id = p.id)",
                (repo, head_sha),
            ).fetchall()

    def repo_installation(self, repo: str) -> int | None:
        with self.read() as conn:
            row = conn.execute("SELECT installation_id FROM repositories WHERE full_name = ?",
                               (repo,)).fetchone()
            return row["installation_id"] if row else None
