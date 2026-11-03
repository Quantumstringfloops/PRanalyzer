from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from github import GithubException, RateLimitExceededException, UnknownObjectException

from pr_analyzer.exceptions import (
    InvalidPRURLError,
    InvalidTokenError,
    PRAnalyzerError,
    PRNotFoundError,
    PrivateRepoError,
    RateLimitError,
    RepositoryNotFoundError,
)
from pr_analyzer.github.client import GithubClient
from pr_analyzer.models import PRSummary, Suggestion

URL = "https://github.com/microsoft/vscode/pull/1"


def make_file(filename="app.py", status="modified", patch="@@ -1,1 +1,1 @@\n+x = 1"):
    return SimpleNamespace(filename=filename, status=status, additions=1, deletions=0, patch=patch)


def make_content(text="x = 1\n", encoding="base64"):
    return SimpleNamespace(encoding=encoding, decoded_content=text.encode())


@pytest.fixture
def github():
    with patch("pr_analyzer.github.client.Github") as mock_github:
        mock_pr = MagicMock()
        mock_pr.number = 1
        mock_pr.title = "Fix bug"
        mock_pr.user.login = "neil"
        mock_pr.base.ref = "main"
        mock_pr.head.ref = "fix-branch"
        mock_pr.head.sha = "abc123"
        mock_pr.draft = False
        mock_pr.additions = 10
        mock_pr.deletions = 5
        mock_pr.get_files.return_value = []

        mock_repo = MagicMock()
        mock_repo.full_name = "microsoft/vscode"
        mock_repo.get_pull.return_value = mock_pr
        mock_repo.get_contents.return_value = make_content()

        mock_github.return_value.get_repo.return_value = mock_repo
        yield SimpleNamespace(github=mock_github, repo=mock_repo, pr=mock_pr)


# ── URL validation ────────────────────────────────────────────────────────────
def test_valid_url():
    client = GithubClient(token="t")
    repo, number = client.parse_url_and_validate("https://github.com/microsoft/vscode/pull/12345")
    assert repo == "microsoft/vscode"
    assert number == 12345

def test_files_tab_url_accepted():
    client = GithubClient(token="t")
    url = "https://github.com/microsoft/vscode/pull/12345/files"
    assert client.parse_url_and_validate(url) == ("microsoft/vscode", 12345)

def test_invalid_url_no_pull():
    client = GithubClient(token="t")
    with pytest.raises(ValueError):
        client.parse_url_and_validate("https://github.com/microsoft/vscode")

def test_invalid_url_not_github():
    client = GithubClient(token="t")
    with pytest.raises(ValueError):
        client.parse_url_and_validate("https://gitlab.com/microsoft/vscode/pull/1")

@pytest.mark.parametrize("url", [
    "http://github.com/microsoft/vscode/pull/1",
    "https://github.com/microsoft/vscode/pull/abc",
    "https://github.com/microsoft/vscode/issues/1",
    "",
])
def test_invalid_urls_raise_analyzer_error(url):
    with pytest.raises(InvalidPRURLError):
        GithubClient(token="t").parse_url_and_validate(url)

# ── fetch_pr ──────────────────────────────────────────────────────────────────
def test_fetch_pr_returns_pr_summary(github):
    result = GithubClient(token="t").fetch_pr(URL)

    assert result.number == 1
    assert result.title == "Fix bug"
    assert result.author == "neil"
    assert result.head_sha == "abc123"

def test_long_patch_is_kept_whole(github):
    # patches over MAX_DIFF_LINES used to crash with TypeError in the truncation code
    patch_text = "@@ -1,0 +1,5000 @@\n" + "\n".join(f"+line {i}" for i in range(5000))
    github.pr.get_files.return_value = [make_file(patch=patch_text)]

    result = GithubClient(token="t").fetch_pr(URL)

    assert result.files[0].patch == patch_text

def test_file_without_patch_is_skipped(github):
    github.pr.get_files.return_value = [make_file(patch=None)]
    assert GithubClient(token="t").fetch_pr(URL).files == []

def test_removed_file_has_no_content(github):
    github.pr.get_files.return_value = [make_file(status="removed")]

    result = GithubClient(token="t").fetch_pr(URL)

    assert result.files[0].raw_content is None
    github.repo.get_contents.assert_not_called()

def test_content_fetched_at_head_commit(github):
    github.pr.get_files.return_value = [make_file()]

    result = GithubClient(token="t").fetch_pr(URL)

    assert result.files[0].raw_content == "x = 1\n"
    github.repo.get_contents.assert_called_once_with("app.py", ref="abc123")

def test_file_over_1mb_has_no_content(github):
    # the contents API returns files over 1 MB without base64 data
    github.pr.get_files.return_value = [make_file()]
    github.repo.get_contents.return_value = make_content(encoding="none")

    assert GithubClient(token="t").fetch_pr(URL).files[0].raw_content is None

def test_missing_content_does_not_abort_fetch(github):
    github.pr.get_files.return_value = [make_file()]
    github.repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})

    assert GithubClient(token="t").fetch_pr(URL).files[0].raw_content is None

def test_missing_repo(github):
    github.github.return_value.get_repo.side_effect = GithubException(404, {"message": "Not Found"}, {})
    with pytest.raises(RepositoryNotFoundError):
        GithubClient(token="t").fetch_pr(URL)

def test_missing_pr(github):
    github.repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    with pytest.raises(PRNotFoundError):
        GithubClient(token="t").fetch_pr(URL)

def test_bad_token(github):
    github.github.return_value.get_repo.side_effect = GithubException(401, {"message": "Bad credentials"}, {})
    with pytest.raises(InvalidTokenError):
        GithubClient(token="t").fetch_pr(URL)

def test_forbidden(github):
    github.github.return_value.get_repo.side_effect = GithubException(403, {"message": "Forbidden"}, {})
    with pytest.raises(PrivateRepoError):
        GithubClient(token="t").fetch_pr(URL)

def test_rate_limit_reports_reset_time(github):
    github.github.return_value.get_repo.side_effect = RateLimitExceededException(
        403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Reset": "1767225600"}
    )
    with pytest.raises(RateLimitError) as excinfo:
        GithubClient(token="t").fetch_pr(URL)
    assert excinfo.value.reset_time == datetime.fromtimestamp(1767225600)

def test_network_error_becomes_analyzer_error(github):
    github.github.return_value.get_repo.side_effect = requests.exceptions.ConnectionError("down")
    with pytest.raises(PRAnalyzerError):
        GithubClient(token="t").fetch_pr(URL)

# ── fetch_file: .pr-analyzer.toml on the base branch ──────────────────────────
CONFIG = ".pr-analyzer.toml"

def test_fetch_file_reads_the_given_ref(github):
    github.repo.get_contents.return_value = make_content('disable = ["llm"]\n')

    raw = GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main")

    assert raw == b'disable = ["llm"]\n'
    github.github.return_value.get_repo.assert_called_with("microsoft/vscode")
    github.repo.get_contents.assert_called_once_with(CONFIG, ref="main")

def test_fetch_file_missing_means_none(github):
    github.repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
    assert GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main") is None

def test_fetch_file_directory_means_none(github):
    github.repo.get_contents.return_value = [make_content()]
    assert GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main") is None

def test_fetch_file_other_errors_are_not_ignored(github):
    # silently using the defaults could drop settings the project chose, e.g. a stricter block_on
    github.repo.get_contents.side_effect = GithubException(500, {"message": "Server Error"}, {})
    with pytest.raises(PRAnalyzerError, match=r"Could not read \.pr-analyzer\.toml at main"):
        GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main")

def test_fetch_file_rate_limit_says_when_to_retry(github):
    github.repo.get_contents.side_effect = RateLimitExceededException(
        403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Reset": "1767225600"}
    )
    with pytest.raises(RateLimitError):
        GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main")

def test_fetch_file_over_1mb_is_an_error(github):
    github.repo.get_contents.return_value = make_content(encoding="none")
    with pytest.raises(PRAnalyzerError, match="over 1 MB"):
        GithubClient(token="t").fetch_file("microsoft/vscode", CONFIG, "main")

def test_no_token_uses_unauthenticated_client():
    with patch("pr_analyzer.github.client.Github") as mock_github:
        GithubClient(token="")
    assert mock_github.call_args.kwargs["auth"] is None

# ── post_review ───────────────────────────────────────────────────────────────
def make_pr_summary():
    return PRSummary(
        number=1, title="Fix bug", author="neil", repo="microsoft/vscode",
        base_branch="main", head_branch="fix-branch", total_additions=1,
        total_deletions=0, is_draft=False, head_sha="abc123",
    )

def make_suggestion():
    return Suggestion(filename="app.py", severity="HIGH", category="security",
                      title="eval() usage detected", description="Avoid eval.", line=3)  # pr-analyzer: ignore

def test_post_review_sends_line_comments(github):
    GithubClient(token="t").post_review(make_pr_summary(), "body", [(make_suggestion(), 3)])

    kwargs = github.pr.create_review.call_args.kwargs
    assert kwargs["event"] == "COMMENT"
    assert kwargs["comments"] == [{
        "path": "app.py",
        "line": 3,
        "side": "RIGHT",
        "body": "**HIGH** — eval() usage detected\n\nAvoid eval.",  # pr-analyzer: ignore
    }]
    github.repo.get_commit.assert_called_once_with("abc123")

def test_post_review_falls_back_to_summary_when_comments_rejected(github):
    github.pr.create_review.side_effect = [GithubException(422, {"message": "Unprocessable"}, {}), None]

    GithubClient(token="t").post_review(make_pr_summary(), "body", [(make_suggestion(), 3)])

    assert github.pr.create_review.call_count == 2
    assert "comments" not in github.pr.create_review.call_args.kwargs

def test_comment_text_from_the_pr_is_neutralised(github):
    # LLM titles and Semgrep messages can carry text a PR author planted
    hostile = Suggestion(filename="app.py", severity="HIGH", category="security", line=3,
                         title="Ask @admin\n## Approved",
                         description="<!-- pr-analyzer --> see [docs](https://evil.example)")

    GithubClient(token="t").post_review(make_pr_summary(), "body", [(hostile, 3)])

    body = github.pr.create_review.call_args.kwargs["comments"][0]["body"]
    assert body.startswith("**HIGH** — Ask @​admin ## Approved\n\n")
    assert "<!--" not in body and "](" not in body and "https://" not in body

def test_post_review_needs_token():
    with pytest.raises(InvalidTokenError):
        GithubClient(token="").post_review(make_pr_summary(), "body", [])

# ── repeat runs on the same PR ────────────────────────────────────────────────
MARKER = "<!-- pr-analyzer -->"

def finding(title="Risky call", line=3):
    return Suggestion(filename="app.py", severity="HIGH", category="security",
                      title=title, description="Details.", line=line)

def posted_comment(title="Risky call", line=3):
    return SimpleNamespace(path="app.py", line=line, body=f"**HIGH** — {title}\n\nDetails.")

def test_first_run_posts_a_tagged_summary_with_its_comments(github):
    GithubClient(token="t").post_review(make_pr_summary(), "summary", [(finding(), 3)])

    kwargs = github.pr.create_review.call_args.kwargs
    assert kwargs["body"] == f"summary\n\n{MARKER}"
    assert [c["line"] for c in kwargs["comments"]] == [3]

def test_rerun_updates_the_summary_instead_of_adding_a_review(github):
    # every push used to add a whole new review that repeated every comment
    earlier = MagicMock(body=f"old summary\n\n{MARKER}")
    github.pr.get_reviews.return_value = [earlier]
    github.pr.get_review_comments.return_value = [posted_comment()]

    GithubClient(token="t").post_review(make_pr_summary(), "new summary", [(finding(), 3)])

    earlier.edit.assert_called_once_with(f"new summary\n\n{MARKER}")
    github.pr.create_review.assert_not_called()

def test_rerun_comments_only_on_new_findings(github):
    github.pr.get_reviews.return_value = [MagicMock(body=f"old\n\n{MARKER}")]
    github.pr.get_review_comments.return_value = [posted_comment()]

    GithubClient(token="t").post_review(
        make_pr_summary(), "summary", [(finding(), 3), (finding("Other risk", 7), 7)]
    )

    kwargs = github.pr.create_review.call_args.kwargs
    assert [c["line"] for c in kwargs["comments"]] == [7]
    assert "1 new finding(s)" in kwargs["body"]

def test_outdated_comments_do_not_count_as_posted(github):
    # GitHub clears `line` on a comment once the code under it changes
    github.pr.get_review_comments.return_value = [posted_comment(line=None)]

    GithubClient(token="t").post_review(make_pr_summary(), "summary", [(finding(), 3)])

    assert [c["line"] for c in github.pr.create_review.call_args.kwargs["comments"]] == [3]

def test_reviews_without_the_tag_are_never_edited(github):
    human = MagicMock(body="Looks good to me")
    github.pr.get_reviews.return_value = [human]

    GithubClient(token="t").post_review(make_pr_summary(), "summary", [])

    human.edit.assert_not_called()
    github.pr.create_review.assert_called_once()

def test_summary_from_another_account_is_left_alone(github):
    # someone else's pr-analyzer review can't be edited with this token; post our own instead
    theirs = MagicMock(body=f"theirs\n\n{MARKER}")
    theirs.edit.side_effect = GithubException(403, {"message": "Forbidden"}, {})
    github.pr.get_reviews.return_value = [theirs]

    GithubClient(token="t").post_review(make_pr_summary(), "mine", [])

    assert github.pr.create_review.call_args.kwargs["body"] == f"mine\n\n{MARKER}"
