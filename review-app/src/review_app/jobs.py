"""A durable job queue in SQLite.

- One analysis per PR at a time: a newer push supersedes the PR's queued analyses, and a job is
  claimed only while no other job for its PR is running.
- A job that fails for a passing reason (a rate limit, GitHub down) goes back on the queue with a
  delay, up to MAX_ATTEMPTS tries.
- A worker that dies mid-job leaves it "running" with a lease; once the lease runs out,
  recover_expired() puts the job back."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from review_app.db import Store, clip, stamp, utcnow

MAX_ATTEMPTS = 3
# longer than any one analysis takes: Semgrep and pip-audit get 5 minutes each, the LLM 2 per request
LEASE = timedelta(minutes=30)
STATUSES = ("queued", "running", "done", "failed", "superseded", "cancelled")


@dataclass(frozen=True)
class Job:
    id: int
    kind: str              # "analyze" | "merge"
    repo: str
    pr_number: int
    head_sha: str
    installation_id: int | None
    attempts: int          # including the current one, once claimed

    @property
    def out_of_attempts(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


class JobQueue:
    def __init__(self, store: Store, clock=utcnow):
        self.store = store
        self.clock = clock

    def _now(self) -> str:
        return stamp(self.clock())

    def enqueue(self, kind: str, repo: str, pr_number: int, head_sha: str,
                installation_id: int | None) -> int | None:
        """Queue a job; None when the same job is already waiting."""
        now = self._now()
        with self.store.transaction() as conn:
            if kind == "analyze":
                # the newest push wins: an older head's analysis would be wasted work
                conn.execute(
                    "UPDATE jobs SET status = 'superseded', updated_at = ? WHERE kind = 'analyze'"
                    " AND repo = ? AND pr_number = ? AND status = 'queued' AND head_sha != ?",
                    (now, repo, pr_number, head_sha),
                )
            duplicate = conn.execute(
                "SELECT id FROM jobs WHERE kind = ? AND repo = ? AND pr_number = ? AND head_sha = ?"
                " AND status = 'queued'", (kind, repo, pr_number, head_sha),
            ).fetchone()
            if duplicate:
                return None
            cursor = conn.execute(
                "INSERT INTO jobs (kind, repo, pr_number, head_sha, installation_id, status,"
                " run_after, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (kind, repo, pr_number, head_sha, installation_id, now, now, now),
            )
            return int(cursor.lastrowid or 0)

    def cancel_for_pr(self, repo: str, pr_number: int) -> int:
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE repo = ? AND"
                " pr_number = ? AND status = 'queued'", (self._now(), repo, pr_number),
            )
            return cursor.rowcount

    def claim(self) -> Job | None:
        now = self.clock()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM jobs j WHERE status = 'queued' AND run_after <= ?"
                " AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status = 'running'"
                "   AND r.repo = j.repo AND r.pr_number = j.pr_number)"
                " ORDER BY run_after, id LIMIT 1", (stamp(now),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_until = ?,"
                " updated_at = ? WHERE id = ?", (stamp(now + LEASE), stamp(now), row["id"]),
            )
        return Job(id=row["id"], kind=row["kind"], repo=row["repo"], pr_number=row["pr_number"],
                   head_sha=row["head_sha"], installation_id=row["installation_id"],
                   attempts=row["attempts"] + 1)

    def finish(self, job: Job, status: str = "done", error: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown job status {status!r}")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, last_error = ?, locked_until = NULL, updated_at = ?"
                " WHERE id = ?", (status, clip(error) if error else None, self._now(), job.id),
            )

    def retry_later(self, job: Job, error: str, delay: timedelta) -> bool:
        """Put the job back after `delay`; False (and the job failed) once it's out of tries."""
        if job.out_of_attempts:
            self.finish(job, "failed", error)
            return False
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'queued', run_after = ?, locked_until = NULL,"
                " last_error = ?, updated_at = ? WHERE id = ?",
                (stamp(self.clock() + delay), clip(error), self._now(), job.id),
            )
        return True

    def recover_expired(self) -> int:
        """Jobs whose worker died: back on the queue, or failed once out of tries."""
        now = self._now()
        with self.store.transaction() as conn:
            failed = conn.execute(
                "UPDATE jobs SET status = 'failed', last_error = 'the worker stopped mid-job too"
                " many times', locked_until = NULL, updated_at = ? WHERE status = 'running' AND"
                " locked_until < ? AND attempts >= ?", (now, now, MAX_ATTEMPTS),
            ).rowcount
            requeued = conn.execute(
                "UPDATE jobs SET status = 'queued', locked_until = NULL, updated_at = ?"
                " WHERE status = 'running' AND locked_until < ?", (now, now),
            ).rowcount
        return failed + requeued

    def counts(self) -> dict[str, int]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            found = {row["status"]: row["n"] for row in rows}
        return {status: found.get(status, 0) for status in STATUSES}


def backoff(attempt: int, base: timedelta = timedelta(minutes=1)) -> timedelta:
    """1, 4, 16 minutes ... for attempts 1, 2, 3."""
    return base * (4 ** max(attempt - 1, 0))


def until(moment: datetime, now: datetime, ceiling: timedelta = timedelta(hours=1)) -> timedelta:
    """The wait until `moment`, at least a minute and at most `ceiling`."""
    return min(max(moment - now, timedelta(minutes=1)), ceiling)
