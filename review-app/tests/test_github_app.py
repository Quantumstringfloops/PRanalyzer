from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import SHA
from github import GithubException, RateLimitExceededException

from review_app.github_app import (
    CHECK_NAME,
    AppAccess,
    InstallationTokens,
    PyGithubGateway,
    TokenAccess,
    pull_from_api,
)


class FakeIntegration:
    def __init__(self):
        self.calls = 0

    def get_access_token(self, installation_id):
        self.calls += 1
        return SimpleNamespace(token=f"ghs_{installation_id}_{self.calls}",
                               expires_at=datetime.fromtimestamp(1000 + 3600, UTC))


def test_installation_tokens_are_cached_until_shortly_before_they_expire():
    now = [1000.0]
    tokens = InstallationTokens(1, "key", integration=FakeIntegration(), clock=lambda: now[0])
    assert tokens.token(5) == "ghs_5_1"
    assert tokens.token(5) == "ghs_5_1"
    now[0] = 1000 + 3600 - 299          # inside the five minutes before it expires
    assert tokens.token(5) == "ghs_5_2"
    assert tokens.token(6) == "ghs_6_3"


def test_the_app_acts_only_on_installations():
    access = AppAccess(InstallationTokens(1, "key", integration=FakeIntegration()))
    assert access.checks
    with pytest.raises(LookupError, match="isn't installed"):
        access.token(None)


def test_a_personal_token_cannot_post_check_runs():
    access = TokenAccess("ghp_x")
    assert not access.checks
    assert access.token(None) == "ghp_x"
    assert TokenAccess("").token(None) is None


def api_pr(**changes):
    values = {
        "number": 7, "title": "Add a helper", "user": SimpleNamespace(login="dev"),
        "head": SimpleNamespace(sha=SHA, repo=SimpleNamespace(id=1)),
        "base": SimpleNamespace(ref="main", repo=SimpleNamespace(id=1, default_branch="main")),
        "draft": False, "merged": False, "state": "open", "changed_files": 2, "additions": 3,
        "deletions": 1,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_pull_from_api():
    pull = pull_from_api("octo/app", api_pr())
    assert (pull.repo, pull.number, pull.author, pull.head_sha, pull.base_branch,
            pull.default_branch, pull.state, pull.changed_files) == (
        "octo/app", 7, "dev", SHA, "main", "main", "open", 2)
    assert not pull.is_fork


def test_a_deleted_fork_counts_as_a_fork():
    pull = pull_from_api("octo/app", api_pr(head=SimpleNamespace(sha=SHA, repo=None)))
    assert pull.head_repo_id is None
    assert pull.is_fork


def test_a_merged_pr_says_so():
    assert pull_from_api("octo/app", api_pr(merged=True, state="closed")).state == "merged"


class FakePull:
    def __init__(self, error=None):
        self.error = error
        self.kwargs = None

    def merge(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(merged=True, message="Pull Request successfully merged",
                               sha="c" * 40)


def gateway_with(pull: FakePull) -> PyGithubGateway:
    gateway = PyGithubGateway(None)
    gateway._pull = lambda repo, number: pull
    return gateway


def test_merges_through_the_merge_api_pinned_to_the_commit():
    pull = FakePull()
    result = gateway_with(pull).merge("octo/app", 7, sha=SHA, method="squash", title="T (#7)")
    assert result.merged and result.sha == "c" * 40
    assert pull.kwargs == {"commit_title": "T (#7)", "merge_method": "squash", "sha": SHA}


@pytest.mark.parametrize(("status", "message", "pending"), [
    (405, 'Required status check "ci" is expected.', True),
    (405, "At least 1 approving review is required by reviewers with write access.", False),
    (409, "Head branch was modified. Review and try the merge again.", False),
    (403, "Resource not accessible by integration", False),
])
def test_a_refusal_is_an_answer_not_an_error(status, message, pending):
    error = GithubException(status, {"message": message}, None)
    result = gateway_with(FakePull(error)).merge("octo/app", 7, SHA, "squash", "T")
    assert not result.merged
    assert result.checks_pending is pending
    assert message in result.message and f"HTTP {status}" in result.message


@pytest.mark.parametrize("error", [
    GithubException(502, {"message": "Bad gateway"}, None),
    RateLimitExceededException(403, {"message": "API rate limit exceeded"}, {}),
])
def test_other_failures_are_raised(error):
    with pytest.raises(GithubException):
        gateway_with(FakePull(error)).merge("octo/app", 7, SHA, "squash", "T")


def test_check_runs_are_completed_with_the_conclusion():
    created = {}
    gateway = PyGithubGateway(None)
    gateway._github = SimpleNamespace(get_repo=lambda name: SimpleNamespace(
        create_check_run=lambda **kwargs: created.update(kwargs)))
    gateway.create_check_run("octo/app", SHA, "neutral", "t" * 300, "summary")
    assert created["name"] == CHECK_NAME
    assert (created["head_sha"], created["status"], created["conclusion"]) == (
        SHA, "completed", "neutral")
    assert len(created["output"]["title"]) == 255
