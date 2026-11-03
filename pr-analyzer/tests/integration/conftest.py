import pytest

from pr_analyzer.github.client import GithubClient


@pytest.fixture(autouse=True)
def no_base_branch_config(monkeypatch):
    # The pipeline also reads .pr-analyzer.toml from the PR's base branch through the GitHub API.
    # Tests that stub fetch_pr must not reach GitHub for it either: no file means the defaults.
    # The tests of that lookup patch fetch_file on their own pipeline.
    monkeypatch.setattr(GithubClient, "fetch_file", lambda self, repo, path, ref: None)
