from datetime import UTC, datetime, timedelta

from conftest import NEW_SHA, SHA

from review_app.jobs import LEASE, MAX_ATTEMPTS, JobQueue, backoff, until


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def status_of(store, job_id: int) -> str:
    with store.read() as conn:
        return conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_a_job_is_claimed_once(queue):
    job_id = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    job = queue.claim()
    assert (job.id, job.kind, job.repo, job.pr_number, job.head_sha, job.installation_id,
            job.attempts) == (job_id, "analyze", "octo/app", 7, SHA, 11, 1)
    assert queue.claim() is None
    assert status_of(queue.store, job_id) == "running"


def test_a_newer_push_supersedes_the_queued_analysis(queue):
    old = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    assert queue.enqueue("analyze", "octo/app", 7, SHA, 11) is None   # already waiting
    new = queue.enqueue("analyze", "octo/app", 7, NEW_SHA, 11)
    assert status_of(queue.store, old) == "superseded"
    assert queue.claim().id == new


def test_one_job_per_pr_runs_at_a_time(queue):
    first = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    queue.claim()
    queue.enqueue("merge", "octo/app", 7, SHA, 11)
    other = queue.enqueue("analyze", "octo/app", 8, SHA, 11)
    assert queue.claim().id == other            # PR 7 is busy
    assert queue.claim() is None
    queue.finish(type("J", (), {"id": first})(), "done")
    assert queue.claim().kind == "merge"


def test_a_retry_waits_its_turn(store):
    clock = Clock()
    queue = JobQueue(store, clock=clock)
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    job = queue.claim()
    assert queue.retry_later(job, "rate limited", timedelta(minutes=4))
    assert queue.claim() is None
    clock.advance(timedelta(minutes=4))
    again = queue.claim()
    assert (again.id, again.attempts) == (job.id, 2)


def test_a_job_out_of_tries_fails(store):
    clock = Clock()
    queue = JobQueue(store, clock=clock)
    job_id = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    for _ in range(MAX_ATTEMPTS):
        job = queue.claim()
        retried = queue.retry_later(job, "GitHub is down", timedelta(0))
    assert not retried
    assert status_of(store, job_id) == "failed"
    with store.read() as conn:
        assert conn.execute("SELECT last_error FROM jobs").fetchone()[0] == "GitHub is down"


def test_a_dead_workers_jobs_come_back(store):
    clock = Clock()
    queue = JobQueue(store, clock=clock)
    job_id = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    queue.claim()
    assert queue.recover_expired() == 0          # still within its lease
    clock.advance(LEASE + timedelta(seconds=1))
    assert queue.recover_expired() == 1
    assert status_of(store, job_id) == "queued"


def test_a_job_that_keeps_killing_workers_fails(store):
    clock = Clock()
    queue = JobQueue(store, clock=clock)
    job_id = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    for _ in range(MAX_ATTEMPTS):
        queue.claim()
        clock.advance(LEASE + timedelta(seconds=1))
        queue.recover_expired()
    assert status_of(store, job_id) == "failed"


def test_closing_a_pr_cancels_its_queued_jobs(queue):
    job_id = queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    assert queue.cancel_for_pr("octo/app", 7) == 1
    assert status_of(queue.store, job_id) == "cancelled"
    assert queue.claim() is None


def test_counts(queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    queue.enqueue("analyze", "octo/app", 8, SHA, 11)
    queue.claim()
    counts = queue.counts()
    assert (counts["queued"], counts["running"], counts["failed"]) == (1, 1, 0)


def test_delays():
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(minutes=m) for m in (1, 4, 16)]
    now = datetime(2026, 11, 8, tzinfo=UTC)
    assert until(now + timedelta(seconds=5), now) == timedelta(minutes=1)
    assert until(now + timedelta(minutes=30), now) == timedelta(minutes=30)
    assert until(now + timedelta(days=1), now) == timedelta(hours=1)
