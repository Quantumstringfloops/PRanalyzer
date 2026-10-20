import pytest
from unittest.mock import MagicMock, patch
from pr_analyzer.github.client import GithubClient
from pr_analyzer.exceptions import InvalidTokenError, PRNotFoundError, RateLimitError

# ── URL validation ────────────────────────────────────────────────────────────
def test_valid_url():
    client = GithubClient()
    repo, number = client.parse_url_and_validate("https://github.com/microsoft/vscode/pull/12345")
    assert repo == "microsoft/vscode"
    assert number == 12345

def test_invalid_url_no_pull():
    client = GithubClient()
    with pytest.raises(ValueError):
        client.parse_url_and_validate("https://github.com/microsoft/vscode")

def test_invalid_url_not_github():
    client = GithubClient()
    with pytest.raises(ValueError):
        client.parse_url_and_validate("https://gitlab.com/microsoft/vscode/pull/1")

# ── fetch_pr ──────────────────────────────────────────────────────────────────
def test_fetch_pr_returns_pr_summary():
    with patch("pr_analyzer.github.client.Github") as mock_github:
        mock_pr = MagicMock()
        mock_pr.number = 1
        mock_pr.title = "Fix bug"
        mock_pr.user.login = "neil"
        mock_pr.base.ref = "main"
        mock_pr.head.ref = "fix-branch"
        mock_pr.draft = False
        mock_pr.additions = 10
        mock_pr.deletions = 5
        mock_pr.get_files.return_value = []

        mock_repo = MagicMock()
        mock_repo.full_name = "microsoft/vscode"
        mock_repo.get_pull.return_value = mock_pr

        mock_github.return_value.get_repo.return_value = mock_repo

        client = GithubClient()
        result = client.fetch_pr("https://github.com/microsoft/vscode/pull/1")

        assert result.number == 1
        assert result.title == "Fix bug"
        assert result.author == "neil"