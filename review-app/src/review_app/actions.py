"""Carrying out a decision: a check run on the analysed commit, then, depending on the repository's
mode, a merge or a review request. Every step lands in the audit log.

A merge happens only when all of these hold:
- the decision is merge;
- the repository's mode is "auto";
- a fresh read of the PR shows it open, not a draft, not from a fork, on an allowed base branch,
  with its head still at the analysed commit;
- GitHub's merge API accepts it. The call is pinned to that commit, and branch protection and
  required checks apply as for anyone.
"""
import requests
import structlog
from github import GithubException
from pr_analyzer.github.markdown import neutralise_line

from review_app.db import Store, clip
from review_app.decision import Decision, Verdict
from review_app.github_app import GitHubGateway
from review_app.models import PullInfo
from review_app.policy import Policy

log = structlog.get_logger(__name__)

CONCLUSIONS = {Verdict.MERGE: "success", Verdict.NEEDS_REVIEW: "neutral", Verdict.BLOCK: "failure"}
VERDICT_LABELS = {Verdict.MERGE: "merge", Verdict.NEEDS_REVIEW: "needs human review",
                  Verdict.BLOCK: "block"}
# a merge GitHub holds back for pending checks is tried again when checks finish, this many times
MAX_MERGE_ATTEMPTS = 5
GITHUB_ERRORS = (GithubException, requests.exceptions.RequestException)


def check_run_title(verdict: Verdict, mode: str) -> str:
    if verdict is Verdict.MERGE:
        return "Ready to merge" if mode == "auto" else "Merge recommended (recommend-only mode)"
    return "Needs human review" if verdict is Verdict.NEEDS_REVIEW else "Blocked"


def check_run_summary(decision: Decision, mode: str) -> str:
    # the reasons quote file names and finding titles from the PR: neutralised, like
    # pr-analyzer's own review, so they can't mention people, link out or hide text
    facts = decision.facts
    lines = [f"**Decision:** {VERDICT_LABELS[decision.verdict]}, "
             + f"confidence {decision.confidence:.2f}"]
    if "score" in facts:
        lines.append(f"**Score:** {facts['score']:.1f} ({facts['grade']})")
    lines.append("**Mode:** " + ("auto: the app merges PRs it decides to merge" if mode == "auto"
                                 else "recommend-only: the app merges nothing"))
    lines += ["", "**Why:**"] + [f"- {neutralise_line(reason)}" for reason in decision.reasons]
    return "\n".join(lines)


def merge_gate_problems(fresh: PullInfo, analysed_sha: str, policy: Policy) -> list[str]:
    """Why the PR, as it is now, must not be merged; empty when it may be."""
    problems = []
    if policy.mode != "auto":
        problems.append("auto-merge is off for this repository")
    if fresh.state != "open":
        problems.append(f"the PR is {fresh.state}")
    if fresh.is_draft:
        problems.append("the PR is a draft")
    if fresh.is_fork:
        problems.append("the PR comes from a fork")
    if fresh.head_sha != analysed_sha:
        problems.append("new commits arrived after the analysis")
    if not policy.allows_base(fresh.base_branch, fresh.default_branch):
        problems.append(f"the PR targets {fresh.base_branch}, a branch auto-merge doesn't use")
    return problems


class Executor:
    def __init__(self, store: Store, actor: str = "worker"):
        self.store = store
        self.actor = actor

    def _audit(self, action: str, repo: str, number: int, sha: str, **details) -> None:
        self.store.audit(self.actor, action, repo, number, sha, **details)

    def carry_out(self, gateway: GitHubGateway, *, checks: bool, decision_id: int,
                  decision: Decision, pull: PullInfo, policy: Policy, analysed_sha: str) -> str:
        """Act on the decision; returns its outcome, which is also stored with it."""
        if checks:
            # before any merge, so a branch protection rule that requires this check sees it pass
            self._check_run(gateway, decision, pull, policy, analysed_sha)

        if decision.verdict is Verdict.MERGE:
            if policy.mode == "auto":
                return self.merge(gateway, decision_id, pull.repo, pull.number, policy,
                                  analysed_sha)
            self._audit("merge.recommended", pull.repo, pull.number, analysed_sha,
                        note="recommend-only mode: nothing was merged")
            outcome = "merge_recommended"
        elif decision.verdict is Verdict.NEEDS_REVIEW:
            outcome = self._request_review(gateway, pull, policy, analysed_sha)
        else:
            outcome = "blocked"
        self.store.set_outcome(decision_id, outcome)
        return outcome

    def merge(self, gateway: GitHubGateway, decision_id: int, repo: str, number: int,
              policy: Policy, analysed_sha: str) -> str:
        def settle(outcome: str, action: str, attempted: bool = False, **details) -> str:
            self._audit(action, repo, number, analysed_sha, **details)
            self.store.set_outcome(decision_id, outcome, merge_attempted=attempted)
            return outcome

        try:
            fresh = gateway.pull(repo, number)
        except GITHUB_ERRORS as e:
            return settle("merge_failed", "merge.failed",
                          error=f"couldn't re-read the PR before merging: {clip(e)}")
        problems = merge_gate_problems(fresh, analysed_sha, policy)
        if problems:
            return settle("merge_aborted", "merge.aborted", problems=problems)

        self._audit("merge.attempted", repo, number, analysed_sha, method=policy.merge_method)
        try:
            result = gateway.merge(repo, number, sha=analysed_sha, method=policy.merge_method,
                                   title=f"{fresh.title} (#{number})")
        except GITHUB_ERRORS as e:
            # not retried: whether GitHub merged it is unknown, and a person should look
            return settle("merge_failed", "merge.failed", attempted=True, error=clip(e))
        if result.merged:
            return settle("merged", "merge.succeeded", attempted=True, merge_commit=result.sha)
        if result.checks_pending:
            return settle("merge_waiting", "merge.waiting", attempted=True,
                          message=clip(result.message))
        return settle("merge_refused", "merge.refused", attempted=True,
                      message=clip(result.message))

    def _check_run(self, gateway: GitHubGateway, decision: Decision, pull: PullInfo,
                   policy: Policy, sha: str) -> None:
        conclusion = CONCLUSIONS[decision.verdict]
        try:
            gateway.create_check_run(pull.repo, sha, conclusion,
                                     check_run_title(decision.verdict, policy.mode),
                                     check_run_summary(decision, policy.mode))
        except GITHUB_ERRORS as e:
            # the decision stands without it; with a required check, GitHub won't merge anyway
            self._audit("check_run.failed", pull.repo, pull.number, sha, error=clip(e))
            return
        self._audit("check_run.created", pull.repo, pull.number, sha, conclusion=conclusion)

    def _request_review(self, gateway: GitHubGateway, pull: PullInfo, policy: Policy,
                        sha: str) -> str:
        users = [u for u in policy.reviewers if u.lower() != pull.author.lower()]
        teams = list(policy.team_reviewers)
        if policy.mode != "auto" or not (users or teams):
            return "needs_review"
        try:
            gateway.request_reviewers(pull.repo, pull.number, users, teams)
        except GITHUB_ERRORS as e:
            self._audit("review.request_failed", pull.repo, pull.number, sha, error=clip(e))
            return "review_request_failed"
        self._audit("review.requested", pull.repo, pull.number, sha, users=users, teams=teams)
        return "review_requested"
