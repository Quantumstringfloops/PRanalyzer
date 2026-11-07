"""The worker: claims jobs and runs analyze → decide → act.

An analysis that fails for a passing reason (a rate limit, GitHub unreachable) is retried later.
One that fails for good still ends in a decision, needs review, so a PR is never left silently
undecided."""
import re
import threading
from contextlib import closing
from datetime import UTC, timedelta
from typing import Any

import requests
import structlog
from github import GithubException, RateLimitExceededException
from pr_analyzer.exceptions import PRAnalyzerError, RateLimitError

from review_app.actions import MAX_MERGE_ATTEMPTS, Executor
from review_app.analysis import Analyzer
from review_app.db import Store, clip, utcnow
from review_app.decision import Decision, Verdict, decide
from review_app.github_app import GitHubAccess, GitHubGateway
from review_app.jobs import Job, JobQueue, backoff, until
from review_app.models import AnalysisOutcome, PullInfo
from review_app.policy import PolicyBook

log = structlog.get_logger(__name__)

MAX_STORED_FINDINGS = 200
# pr-analyzer's wording for GitHub being unreachable or failing on its side
PASSING_ANALYZER_ERRORS = re.compile(r"^(?:Could not reach GitHub|GitHub error 5\d\d)")


class RetryLater(Exception):
    def __init__(self, error: BaseException, delay: timedelta):
        self.delay = delay
        super().__init__(str(error) or repr(error))


def retry_delay(error: BaseException, attempt: int) -> timedelta | None:
    """How long to wait before trying again, or None if trying again won't help."""
    if isinstance(error, RateLimitError):
        return until(error.reset_time.astimezone(UTC), utcnow())
    if isinstance(error, RateLimitExceededException | requests.exceptions.RequestException):
        return backoff(attempt)
    if isinstance(error, GithubException) and (error.status or 0) >= 500:
        return backoff(attempt)
    if isinstance(error, PRAnalyzerError) and PASSING_ANALYZER_ERRORS.match(str(error)):
        return backoff(attempt)
    return None


def finding(s: Any) -> dict[str, Any]:
    return {"filename": s.filename, "line": s.line, "severity": s.severity,
            "category": s.category, "title": clip(s.title, 200),
            "description": clip(s.description, 600), "source": s.source,
            "confidence": s.confidence, "rule_id": s.rule_id}


def analysis_summary(outcome: AnalysisOutcome) -> dict[str, Any]:
    result = outcome.result
    score = result.score
    return {
        "score": score.overall_score, "grade": score.grade, "passed": score.passed,
        "critical_count": score.critical_count, "high_count": score.high_count,
        "dimensions": score.dimension_scores,
        "findings": [finding(s) for s in result.suggestions[:MAX_STORED_FINDINGS]],
        "warnings": result.warnings, "llm_provider": result.llm_provider_used,
        "duration": result.duration,
    }


class Worker:
    def __init__(self, store: Store, queue: JobQueue, policies: PolicyBook, analyzer: Analyzer,
                 access: GitHubAccess, *, skip_llm: bool = False, actor: str = "worker",
                 include_closed: bool = False):
        self.store = store
        self.queue = queue
        self.policies = policies
        self.analyzer = analyzer
        self.access = access
        self.skip_llm = skip_llm
        self.actor = actor
        # analyse closed PRs too: for `review-app analyze`, a report on any PR. The merge gates
        # still refuse a PR that isn't open.
        self.include_closed = include_closed
        self.executor = Executor(store, actor)

    # ── the loop ────────────────────────────────────────────────────────────────────────
    def run_forever(self, stop: threading.Event, poll_seconds: float = 2.0) -> None:
        log.info("worker_started")
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(poll_seconds)
            except Exception:   # a bug in one job must not stop the worker
                log.exception("worker_loop_error")
                stop.wait(poll_seconds)
        log.info("worker_stopped")

    def run_once(self) -> bool:
        """Work on one job; False when there was none ready."""
        job = self.queue.claim()
        if job is None:
            self.queue.recover_expired()
            return False
        self.process(job)
        return True

    def process(self, job: Job) -> None:
        where = (job.repo, job.pr_number, job.head_sha)
        try:
            status = self._analyze(job) if job.kind == "analyze" else self._merge_again(job)
            self.queue.finish(job, status)
        except RetryLater as e:
            if self.queue.retry_later(job, str(e), e.delay):
                self.store.audit(self.actor, "job.retrying", *where, job=job.id,
                                 attempt=job.attempts, error=clip(e),
                                 delay_seconds=int(e.delay.total_seconds()))
            else:
                self.store.audit(self.actor, "job.failed", *where, job=job.id, error=clip(e))
        except Exception as e:
            log.exception("job_failed", job=job.id)
            self.queue.finish(job, "failed", repr(e))
            self.store.audit(self.actor, "job.failed", *where, job=job.id, error=clip(repr(e)))

    # ── analyze → decide → act ──────────────────────────────────────────────────────────
    def _analyze(self, job: Job) -> str:
        policy = self.policies.for_repo(job.repo)
        if not policy.enabled:
            self.store.audit(self.actor, "job.skipped", job.repo, job.pr_number, job.head_sha,
                             job=job.id, reason="the app is switched off for this repository")
            return "done"

        with closing(self._gateway(job)) as gateway:
            pull = self._read_pull(gateway, job)
            if pull.state != "open" and not self.include_closed:
                self.store.audit(self.actor, "job.skipped", job.repo, job.pr_number,
                                 job.head_sha, job=job.id, reason=f"the PR is {pull.state}")
                return "done"
            if job.head_sha and pull.head_sha != job.head_sha:
                # a newer push; make sure it has a job of its own, in case its webhook was lost
                self.queue.enqueue("analyze", job.repo, job.pr_number, pull.head_sha,
                                   job.installation_id)
                self.store.audit(self.actor, "job.superseded", job.repo, job.pr_number,
                                 job.head_sha, job=job.id, newer_head=pull.head_sha)
                return "superseded"

            outcome, error = self._run_analysis(job, pull, policy.post_review)
            analysed_sha = outcome.result.pr.head_sha if outcome else pull.head_sha
            decision = decide(pull, policy, outcome, error)
            decision_id = self._record(pull, analysed_sha, outcome, error, decision, policy.mode)
            self.executor.carry_out(gateway, checks=self.access.checks, decision_id=decision_id,
                                    decision=decision, pull=pull, policy=policy,
                                    analysed_sha=analysed_sha)
        return "done"

    def _gateway(self, job: Job) -> GitHubGateway:
        try:
            return self.access.gateway(job.installation_id)
        except Exception as e:
            delay = retry_delay(e, job.attempts)
            if delay is not None:
                raise RetryLater(e, delay) from e
            raise

    def _read_pull(self, gateway: GitHubGateway, job: Job) -> PullInfo:
        try:
            pull = gateway.pull(job.repo, job.pr_number)
        except Exception as e:
            delay = retry_delay(e, job.attempts)
            if delay is not None:
                raise RetryLater(e, delay) from e
            raise
        with self.store.transaction() as conn:
            self.store.upsert_repo(conn, pull.repo, job.installation_id,
                                   pull.default_branch or None, active=None)
            self.store.upsert_pr(conn, pull)
        return pull

    def _run_analysis(self, job: Job, pull: PullInfo,
                      post_review: bool) -> tuple[AnalysisOutcome | None, str | None]:
        try:
            token = self.access.token(job.installation_id)
            outcome = self.analyzer.analyze(pull, token=token, post_review=post_review,
                                            skip_llm=self.skip_llm)
            return outcome, None
        except Exception as e:
            delay = retry_delay(e, job.attempts)
            if delay is not None and not job.out_of_attempts:
                raise RetryLater(e, delay) from e
            if not isinstance(e, PRAnalyzerError):
                log.exception("analysis_crashed", repo=job.repo, pr=job.pr_number)
            tries = f" (gave up after {job.attempts} tries)" if delay is not None else ""
            return None, clip(f"{e}{tries}" if isinstance(e, PRAnalyzerError) else repr(e))

    def _record(self, pull: PullInfo, analysed_sha: str, outcome: AnalysisOutcome | None,
                error: str | None, decision: Decision, mode: str) -> int:
        with self.store.transaction() as conn:
            pr_id = self.store.upsert_pr(conn, pull)
            summary: dict[str, Any] = analysis_summary(outcome) if outcome else {"error": error}
            analysis_id = self.store.add_analysis(conn, pr_id, analysed_sha, summary)
            if outcome:
                self.store.audit(self.actor, "analysis.completed", pull.repo, pull.number,
                                 analysed_sha, conn=conn, analysis=analysis_id,
                                 score=summary["score"], grade=summary["grade"],
                                 warnings=len(summary["warnings"]),
                                 llm_provider=summary["llm_provider"])
            else:
                self.store.audit(self.actor, "analysis.failed", pull.repo, pull.number,
                                 analysed_sha, conn=conn, analysis=analysis_id, error=error)
            decision_id = self.store.add_decision(
                conn, pr_id, analysis_id, analysed_sha, decision.verdict.value,
                decision.confidence, decision.complete, mode, list(decision.reasons),
                decision.facts,
            )
            self.store.audit(self.actor, "decision.made", pull.repo, pull.number, analysed_sha,
                             conn=conn, decision=decision_id, verdict=decision.verdict.value,
                             confidence=decision.confidence, complete=decision.complete,
                             mode=mode, reasons=list(decision.reasons))
        return decision_id

    # ── a merge that waited for checks ──────────────────────────────────────────────────
    def _merge_again(self, job: Job) -> str:
        policy = self.policies.for_repo(job.repo)
        row = self.store.latest_decision(job.repo, job.pr_number)
        if (row is None or row["verdict"] != Verdict.MERGE.value
                or row["outcome"] != "merge_waiting" or row["head_sha"] != job.head_sha):
            self.store.audit(self.actor, "merge.retry_skipped", job.repo, job.pr_number,
                             job.head_sha, job=job.id,
                             reason="the PR's latest decision isn't a merge waiting for checks")
            return "done"
        if row["merge_attempts"] >= MAX_MERGE_ATTEMPTS:
            self.store.audit(self.actor, "merge.gave_up", job.repo, job.pr_number, job.head_sha,
                             attempts=row["merge_attempts"])
            self.store.set_outcome(row["id"], "merge_refused")
            return "done"
        with closing(self._gateway(job)) as gateway:
            self.executor.merge(gateway, row["id"], job.repo, job.pr_number, policy, job.head_sha)
        return "done"
