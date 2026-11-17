"""The worker: claims jobs and runs analyze → decide → act.

An analysis that fails for a passing reason (a rate limit, GitHub unreachable) is retried later.
One that fails for good still ends in a decision, needs review, so a PR is never left silently
undecided."""
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
import structlog
from github import GithubException, RateLimitExceededException
from pr_analyzer.exceptions import PRAnalyzerError, RateLimitError

from review_app import outcomes, snippets
from review_app.actions import GITHUB_ERRORS, MAX_MERGE_ATTEMPTS, Executor
from review_app.analysis import Analyzer
from review_app.db import Store, clip, stamp, utcnow
from review_app.decision import Decision, Verdict, decide
from review_app.github_app import CHECK_NAME, GitHubAccess, GitHubGateway
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


def finding(s: Any, patch: str | None = None) -> dict[str, Any]:
    """What's stored of a finding, with the diff lines around it from its file's patch."""
    return {"filename": s.filename, "line": s.line, "severity": s.severity,
            "category": s.category, "title": clip(s.title, 200),
            "description": clip(s.description, 600), "source": s.source,
            "confidence": s.confidence, "rule_id": s.rule_id,
            "snippet": snippets.snippet(patch, s.line)}


def analysis_summary(outcome: AnalysisOutcome) -> dict[str, Any]:
    result = outcome.result
    score = result.score
    patches = {f.filename: f.patch for f in result.pr.files}
    return {
        "score": score.overall_score, "grade": score.grade, "passed": score.passed,
        "critical_count": score.critical_count, "high_count": score.high_count,
        "dimensions": score.dimension_scores,
        "findings": [finding(s, patches.get(s.filename))
                     for s in result.suggestions[:MAX_STORED_FINDINGS]],
        "warnings": result.warnings, "llm_provider": result.llm_provider_used,
        "duration": result.duration,
    }


class Worker:
    def __init__(self, store: Store, queue: JobQueue, policies: PolicyBook, analyzer: Analyzer,
                 access: GitHubAccess, *, skip_llm: bool = False, actor: str = "worker",
                 include_closed: bool = False, outcome_comments: bool | None = None,
                 keep_watching: bool = True, clock=utcnow):
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
        # after a merge: comment on the PR (None: only as the GitHub App, if the policy says
        # so), and queue the next look (off for `review-app outcome`, a one-off)
        self.outcome_comments = outcome_comments
        self.keep_watching = keep_watching
        self.clock = clock
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
        steps = {"analyze": self._analyze, "merge": self._merge_again,
                 "outcome": self._look_after_merge}
        try:
            status = steps[job.kind](job)
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

    # ── after the merge: how it turned out ──────────────────────────────────────────────
    def _look_after_merge(self, job: Job) -> str:
        policy = self.policies.for_repo(job.repo)
        if not (policy.enabled and policy.track_outcomes):
            self.store.audit(self.actor, "job.skipped", job.repo, job.pr_number, job.head_sha,
                             job=job.id, reason="outcome tracking is off for this repository")
            return "done"
        with closing(self._gateway(job)) as gateway:
            pull = self._read_pull(gateway, job)
            if pull.state != "merged" or not pull.merge_sha:
                self.store.audit(self.actor, "job.skipped", job.repo, job.pr_number,
                                 job.head_sha, job=job.id, reason=f"the PR is {pull.state}")
                return "done"
            now = self.clock()
            merged_at = parse_time(pull.merged_at) or now
            watch_until = merged_at + timedelta(days=policy.outcome_window_days)
            try:
                outcome = self._observe(gateway, pull, merged_at, final=now >= watch_until)
            except Exception as e:
                delay = retry_delay(e, job.attempts)
                if delay is not None:
                    raise RetryLater(e, delay) from e
                raise
            decided = self.store.latest_decision(pull.repo, pull.number)
            call = outcomes.judge(decided["verdict"] if decided else None, outcome.assessment)
            previous = self.store.merge_outcome(pull.repo, pull.number)
            outcome_id = self._record_outcome(pull, outcome, merged_at, watch_until, decided,
                                              call)
            if self._should_comment(policy, outcome, previous):
                body = outcomes.report(
                    outcome, repo=pull.repo, merge_sha=pull.merge_sha,
                    verdict=decided["verdict"] if decided else None,
                    confidence=decided["confidence"] if decided else None, call=call,
                    watch_until=stamp(watch_until), checked_at=stamp(now))
                self._comment(gateway, pull, outcome_id, body,
                              previous["comment_id"] if previous else None)
        upcoming = outcomes.next_look(now, watch_until, outcome.ci.state)
        if upcoming is not None and self.keep_watching:
            self.queue.enqueue("outcome", pull.repo, pull.number, pull.merge_sha,
                               job.installation_id, run_after=upcoming)
        return "done"

    def _observe(self, gateway: GitHubGateway, pull: PullInfo, merged_at: datetime,
                 final: bool) -> outcomes.Outcome:
        """One look: CI on the merge commit, then the base branch's commits since the merge."""
        ci = outcomes.summarize_checks(gateway.commit_checks(pull.repo, pull.merge_sha),
                                       ignore=(CHECK_NAME,))
        commits = [c for c in gateway.commits_since(pull.repo, pull.base_branch, merged_at,
                                                    outcomes.MAX_COMMITS)
                   if c.sha != pull.merge_sha]
        revert = outcomes.find_revert(commits, pull.merge_sha, pull.repo, pull.number,
                                      pull.title)
        candidates = [c for c in commits if outcomes.looks_like_fix(c.message)
                      and (revert is None or c.sha != revert.sha)]
        follow_ups: tuple[outcomes.FollowUp, ...] = ()
        if candidates:
            pr_files = gateway.pull_files(pull.repo, pull.number, outcomes.MAX_PR_FILES)
            looked_up = [replace(c, files=tuple(gateway.commit_files(pull.repo, c.sha)))
                         for c in candidates[:outcomes.MAX_FILE_LOOKUPS]]
            follow_ups = outcomes.follow_up_fixes(looked_up, pr_files)
        return outcomes.Outcome(outcomes.assess(ci, revert, follow_ups, final), ci, revert,
                                follow_ups, final)

    def _record_outcome(self, pull: PullInfo, outcome: outcomes.Outcome, merged_at: datetime,
                        watch_until: datetime, decided: sqlite3.Row | None, call: str) -> int:
        revert = outcome.revert
        values = {
            "merge_sha": pull.merge_sha, "merged_at": stamp(merged_at),
            "base_branch": pull.base_branch, "assessment": outcome.assessment,
            "ci_state": outcome.ci.state,
            "ci": {"passed": list(outcome.ci.passed), "failed": list(outcome.ci.failed),
                   "pending": list(outcome.ci.pending)},
            "revert_sha": revert.sha if revert else None,
            "revert_subject": clip(revert.subject, 300) if revert else None,
            "follow_ups": [{"sha": f.sha, "subject": clip(f.subject, 300), "files": list(f.files)}
                           for f in outcome.follow_ups],
            "decision_id": decided["id"] if decided else None,
            "verdict": decided["verdict"] if decided else None,
            "verdict_call": call, "final": outcome.final, "watch_until": stamp(watch_until),
        }
        with self.store.transaction() as conn:
            pr_id = self.store.upsert_pr(conn, pull)
            outcome_id = self.store.save_merge_outcome(conn, pr_id, values)
            self.store.audit(self.actor, "outcome.checked", pull.repo, pull.number,
                             pull.merge_sha, conn=conn, assessment=outcome.assessment,
                             ci=outcome.ci.state, reverted=revert is not None,
                             follow_ups=len(outcome.follow_ups), call=call,
                             final=outcome.final)
        return outcome_id

    def _should_comment(self, policy: Any, outcome: outcomes.Outcome,
                        previous: sqlite3.Row | None) -> bool:
        """Whether to post (or update) the comment on the PR. Asked for explicitly (`review-app
        outcome --comment`), always. Otherwise only as the GitHub App, with the policy's
        outcome_comment on, once there's something to say (trouble, or the watch is over), and
        only when that changed since the last comment."""
        if self.outcome_comments is not None:
            return self.outcome_comments
        if not (self.access.checks and policy.outcome_comment):
            return False
        if not (outcome.troubled or outcome.final):
            return False
        if previous is None or previous["comment_id"] is None:
            return True
        return (previous["assessment"], bool(previous["final"])) != (outcome.assessment,
                                                                     outcome.final)

    def _comment(self, gateway: GitHubGateway, pull: PullInfo, outcome_id: int, body: str,
                 comment_id: int | None) -> None:
        try:
            posted = gateway.put_comment(pull.repo, pull.number, body, comment_id,
                                         marker=outcomes.COMMENT_MARKER)
        except GITHUB_ERRORS as e:
            # the outcome is recorded either way; the next look tries the comment again
            self.store.audit(self.actor, "outcome.comment_failed", pull.repo, pull.number,
                             pull.merge_sha, error=clip(e))
            return
        self.store.set_outcome_comment(outcome_id, posted)
        self.store.audit(self.actor, "outcome.commented", pull.repo, pull.number,
                         pull.merge_sha, comment=posted, updated=comment_id is not None)


def parse_time(text: str) -> datetime | None:
    """GitHub's "2026-11-08T15:57:51Z" as an aware datetime; None for anything else."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
