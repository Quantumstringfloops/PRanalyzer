"""GitHub access: the App's installation tokens, and a small gateway for the calls the app makes
itself (pr-analyzer fetches PRs and posts its review on its own).

An installation token lasts an hour. The App signs a JWT with its private key to mint one, and
tokens are cached until five minutes before they expire."""
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from github import Auth, Github, GithubException, GithubIntegration, RateLimitExceededException
from github.PullRequest import PullRequest

from review_app.models import MergeResult, PullInfo

CHECK_NAME = "review-app"   # the check run's name; branch protection can require it
TIMEOUT_SECONDS = 30
REFRESH_MARGIN_SECONDS = 300
# merge API answers that mean "not merged", not "something broke": not allowed (403), gone (404),
# not mergeable, e.g. protections or checks (405), the head moved (409), invalid (422)
MERGE_REFUSALS = frozenset({403, 404, 405, 409, 422})


class GitHubGateway(Protocol):
    def pull(self, repo: str, number: int) -> PullInfo: ...
    def create_check_run(self, repo: str, head_sha: str, conclusion: str, title: str,
                         summary: str) -> None: ...
    def merge(self, repo: str, number: int, sha: str, method: str, title: str) -> MergeResult: ...
    def request_reviewers(self, repo: str, number: int, users: list[str],
                          teams: list[str]) -> None: ...
    def close(self) -> None: ...


class GitHubAccess(Protocol):
    checks: bool   # whether it may create check runs: only a GitHub App can

    def token(self, installation_id: int | None) -> str | None: ...
    def gateway(self, installation_id: int | None) -> GitHubGateway: ...


def error_message(e: GithubException) -> str:
    data = e.data
    if isinstance(data, dict):
        return str(data.get("message") or data)
    return str(data or e)


def pull_from_api(repo: str, pr: PullRequest) -> PullInfo:
    head_repo = pr.head.repo   # None once a fork is deleted
    return PullInfo(
        repo=repo,
        number=pr.number,
        title=pr.title or "",
        author=pr.user.login if pr.user else "",
        head_sha=pr.head.sha,
        base_branch=pr.base.ref,
        default_branch=pr.base.repo.default_branch,
        head_repo_id=head_repo.id if head_repo is not None else None,
        base_repo_id=pr.base.repo.id,
        is_draft=bool(pr.draft),
        state="merged" if pr.merged else pr.state,
        changed_files=pr.changed_files,
        additions=pr.additions,
        deletions=pr.deletions,
    )


class PyGithubGateway:
    def __init__(self, token: str | None):
        # no retries: a merge must not be sent twice, and a failed job is retried as a whole
        self._github = Github(auth=Auth.Token(token) if token else None,
                              timeout=TIMEOUT_SECONDS, retry=None)

    def close(self) -> None:
        self._github.close()

    def _pull(self, repo: str, number: int) -> PullRequest:
        return self._github.get_repo(repo).get_pull(number)

    def pull(self, repo: str, number: int) -> PullInfo:
        return pull_from_api(repo, self._pull(repo, number))

    def create_check_run(self, repo: str, head_sha: str, conclusion: str, title: str,
                         summary: str) -> None:
        self._github.get_repo(repo).create_check_run(
            name=CHECK_NAME, head_sha=head_sha, status="completed", conclusion=conclusion,
            output={"title": title[:255], "summary": summary[:60000]},
        )

    def merge(self, repo: str, number: int, sha: str, method: str, title: str) -> MergeResult:
        try:
            # sha pins the merge to the analysed commit: GitHub refuses it (409) if the head moved
            status = self._pull(repo, number).merge(commit_title=title, merge_method=method,
                                                    sha=sha)
        except RateLimitExceededException:
            raise
        except GithubException as e:
            if e.status not in MERGE_REFUSALS:
                raise
            message = error_message(e)
            return MergeResult(
                merged=False,
                message=f"GitHub refused the merge (HTTP {e.status}): {message}",
                checks_pending=e.status == 405 and "check" in message.lower(),
            )
        return MergeResult(merged=bool(status.merged), message=status.message or "",
                           sha=status.sha)

    def request_reviewers(self, repo: str, number: int, users: list[str],
                          teams: list[str]) -> None:
        self._pull(repo, number).create_review_request(reviewers=users, team_reviewers=teams)


@dataclass
class _Token:
    value: str
    expires_at: float


class InstallationTokens:
    def __init__(self, app_id: int, private_key: str, integration: GithubIntegration | None = None,
                 clock=time.time):
        self._integration = integration or GithubIntegration(
            auth=Auth.AppAuth(app_id, private_key), timeout=TIMEOUT_SECONDS)
        self._clock = clock
        self._tokens: dict[int, _Token] = {}
        self._lock = threading.Lock()

    def token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - REFRESH_MARGIN_SECONDS > self._clock():
                return cached.value
            grant = self._integration.get_access_token(installation_id)
            self._tokens[installation_id] = _Token(grant.token, grant.expires_at.timestamp())
            return grant.token


class AppAccess:
    """As the GitHub App: a token per installation, and check runs."""
    checks = True

    def __init__(self, tokens: InstallationTokens):
        self.tokens = tokens

    def token(self, installation_id: int | None) -> str:
        if installation_id is None:
            raise LookupError("the app isn't installed on this repository")
        return self.tokens.token(installation_id)

    def gateway(self, installation_id: int | None) -> GitHubGateway:
        return PyGithubGateway(self.token(installation_id))


class TokenAccess:
    """With a personal access token (GITHUB_TOKEN), for `review-app analyze`: no check runs, which
    only an App can create."""
    checks = False

    def __init__(self, token: str):
        self._token = token

    def token(self, installation_id: int | None) -> str | None:
        return self._token or None

    def gateway(self, installation_id: int | None) -> GitHubGateway:
        return PyGithubGateway(self._token or None)
