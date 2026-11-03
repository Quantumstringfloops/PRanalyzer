import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from github import GithubException, RateLimitExceededException, UnknownObjectException

from pr_analyzer.cache import DiskCache
from pr_analyzer.config import settings
from pr_analyzer.exceptions import (
    InvalidPRURLError,
    InvalidTokenError,
    PRAnalyzerError,
    PRNotFoundError,
    PrivateRepoError,
    RateLimitError,
    RepositoryNotFoundError,
)
from pr_analyzer.github import client as client_module
from pr_analyzer.github.client import GithubClient
from pr_analyzer.models import PRSummary, Suggestion

URL = "https://github.com/microsoft/vscode/pull/1"
# a test that goes wrong waits this long for another thread, rather than hanging
TIMEOUT = 5


def make_file(filename="app.py", status="modified", patch="@@ -1,1 +1,1 @@\n+x = 1", sha=None):
    return SimpleNamespace(filename=filename, status=status, additions=1, deletions=0, patch=patch,
                           sha=sha)


def make_content(text="x = 1\n", encoding="base64", sha=None):
    return SimpleNamespace(encoding=encoding, decoded_content=text.encode(), sha=sha)


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

# ── fetching contents in parallel ─────────────────────────────────────────────
class FakeRepo:
    def __init__(self, owner, contents):
        self.full_name = "microsoft/vscode"
        self.owner = owner
        self.contents = contents
        self.threads = set()

    def get_contents(self, path, ref):
        self.threads.add(threading.get_ident())
        self.owner.requests.append((path, ref))
        result = self.contents[path]
        if callable(result):
            result = result()   # a request that takes its time
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, threading.Barrier):
            result.wait()   # returns once another worker is fetching at the same time
            return make_content(f"{path}\n")
        return make_content(result, sha=self.owner.served_blobs.get(path))


@pytest.fixture
def fake_github(monkeypatch):
    """Github clients whose repos record which thread used them."""
    state = SimpleNamespace(clients=[], requests=[], contents={}, files=[], served_blobs={})
    pr = MagicMock(number=1, title="t", additions=1, deletions=0, draft=False)
    pr.user.login = "neil"
    pr.head.sha = "abc123"
    pr.get_files.side_effect = lambda: state.files
    state.pr = pr

    class FakeGithub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.repo = FakeRepo(state, state.contents)
            self.repo.get_pull = lambda number: pr
            state.clients.append(self)

        def get_repo(self, name):
            return self.repo

        def close(self):
            self.closed = True

    monkeypatch.setattr("pr_analyzer.github.client.Github", FakeGithub)

    def add(name, result="x = 1\n", status="modified", blob=None):
        state.files.append(make_file(filename=name, status=status, sha=blob))
        state.contents[name] = result
        state.served_blobs[name] = blob   # the blob SHA the contents API answers with

    state.add = add
    return state

def test_contents_are_fetched_at_the_same_time_and_kept_in_order(fake_github):
    both = threading.Barrier(2, timeout=5)   # fails if the two fetches don't overlap
    fake_github.add("a.py", both)
    fake_github.add("b.py", both)
    for name in ("c.py", "d.py", "e.py"):
        fake_github.add(name, f"{name}\n")

    pr = GithubClient(token="t").fetch_pr(URL)

    assert [f.filename for f in pr.files] == ["a.py", "b.py", "c.py", "d.py", "e.py"]
    assert [f.raw_content for f in pr.files] == [f"{f.filename}\n" for f in pr.files]

def test_each_worker_thread_has_its_own_client(fake_github):
    for i in range(6):
        fake_github.add(f"f{i}.py")

    GithubClient(token="t").fetch_pr(URL)

    main, *workers = fake_github.clients
    assert workers and main.repo.threads == set()          # the main client stays on its thread
    assert all(len(w.repo.threads) == 1 for w in workers)  # no client is shared between threads
    assert all(w.closed and w.kwargs["lazy"] for w in workers)

def test_one_worker_fetches_in_turn_on_the_main_client(fake_github, monkeypatch):
    monkeypatch.setattr(settings, "github_max_workers", 1)
    for name in ("a.py", "b.py"):
        fake_github.add(name)

    GithubClient(token="t").fetch_pr(URL)

    [main] = fake_github.clients
    assert main.repo.threads == {threading.get_ident()}

def test_failed_fetch_leaves_that_file_without_content(fake_github):
    fake_github.add("a.py")
    fake_github.add("gone.py", UnknownObjectException(404, {"message": "Not Found"}, {}))
    fake_github.add("c.py")
    fake_github.add("old.py", status="removed")

    pr = GithubClient(token="t").fetch_pr(URL)

    assert [f.raw_content for f in pr.files] == ["x = 1\n", None, "x = 1\n", None]
    assert ("old.py", "abc123") not in fake_github.requests

def test_rate_limit_in_a_worker_fails_the_fetch(fake_github):
    fake_github.add("a.py")
    fake_github.add("b.py", RateLimitExceededException(
        403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Reset": "1767225600"}))

    with pytest.raises(RateLimitError):
        GithubClient(token="t").fetch_pr(URL)
    assert all(c.closed for c in fake_github.clients[1:])

def test_ctrl_c_ends_the_fetch_without_waiting_for_requests_in_flight(fake_github, monkeypatch):
    # Ctrl-C reaches only the main thread, as KeyboardInterrupt while it waits for the workers.
    # It used to wait for every request in flight, and its rate-limit waits, before stopping.
    monkeypatch.setattr(settings, "github_max_workers", 2)
    started, finish = threading.Semaphore(0), threading.Event()
    in_flight = []

    def slow():
        in_flight.append(threading.current_thread())
        started.release()
        finish.wait(TIMEOUT)
        return "x = 1\n"

    for name in ("a.py", "b.py", "c.py", "d.py"):
        fake_github.add(name, slow)

    def ctrl_c(futures, return_when):
        for _ in range(2):
            assert started.acquire(timeout=TIMEOUT)   # both workers are busy
        raise KeyboardInterrupt

    monkeypatch.setattr(client_module, "wait", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        GithubClient(token="t").fetch_pr(URL)

    workers = fake_github.clients[1:]
    assert all(t.is_alive() for t in in_flight) and not finish.is_set()   # not waited for
    assert len(workers) == 2 and all(w.closed for w in workers)
    finish.set()
    for thread in in_flight:
        thread.join(TIMEOUT)
    assert sorted(path for path, _ in fake_github.requests) == ["a.py", "b.py"]   # none queued

# ── content cache ─────────────────────────────────────────────────────────────
def test_cached_contents_are_not_fetched_again(fake_github, tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        fake_github.add(name, f"{name}\n")

    first = GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)
    fetched = len(fake_github.requests)
    second = GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)

    assert fetched == 3 and len(fake_github.requests) == 3
    assert [f.raw_content for f in second.files] == [f.raw_content for f in first.files]

def test_unchanged_file_is_not_fetched_again_after_a_push(fake_github, tmp_path):
    # a push makes a new head commit, but a file that didn't change keeps its blob SHA
    fake_github.add("same.py", blob="b" * 40)
    fake_github.add("edited.py")
    GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)
    fake_github.requests.clear()
    fake_github.contents["edited.py"] = "x = 2\n"

    fake_github.pr.head.sha = "def456"   # the PR's new head commit
    pr = GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)

    assert fake_github.requests == [("edited.py", "def456")]
    assert [f.raw_content for f in pr.files] == ["x = 1\n", "x = 2\n"]

def test_text_of_another_commit_is_not_stored_as_the_blob(fake_github, tmp_path):
    # the PR moved on between listing its files and fetching them: the text isn't that blob's
    fake_github.add("a.py", blob="b" * 40)
    fake_github.served_blobs["a.py"] = "c" * 40

    GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)
    GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)

    assert len(fake_github.requests) == 2

def test_unavailable_contents_are_not_cached(fake_github, tmp_path):
    fake_github.add("gone.py", UnknownObjectException(404, {"message": "Not Found"}, {}))

    GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)
    GithubClient(token="t", cache=DiskCache(tmp_path)).fetch_pr(URL)

    assert len(fake_github.requests) == 2

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

def finding(title="Risky call", line=3, description="Details."):
    return Suggestion(filename="app.py", severity="HIGH", category="security",
                      title=title, description=description, line=line)

def posted_comment(title="Risky call", line=3, description="Details."):
    return SimpleNamespace(path="app.py", line=line, body=f"**HIGH** — {title}\n\n{description}")

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

def test_rate_limit_on_our_summary_is_not_taken_for_another_accounts_review(github):
    # a secondary limit comes as a 403 too: it used to post a second summary review, the
    # duplicate the tag is there to prevent
    ours = MagicMock(body=f"old\n\n{MARKER}")
    ours.edit.side_effect = RateLimitExceededException(
        403, {"message": "You have exceeded a secondary rate limit."}, {"Retry-After": "300"})
    github.pr.get_reviews.return_value = [ours]

    with pytest.raises(RateLimitError):
        GithubClient(token="t").post_review(make_pr_summary(), "new", [(finding(), 3)])

    github.pr.create_review.assert_not_called()

# ── comments an earlier version posted ────────────────────────────────────────
# Versions put zero-width spaces in different places: the one before 0.1.0 put one after every
# "]", "<" and "@", in code too. Compared byte for byte, its comments looked new to the first run
# after an upgrade, which posted them again.
@pytest.mark.parametrize("description, as_posted", [
    # as the version before 0.1.0 posted them
    ("Index with `rows[0]` instead of looping.", "Index with `rows[0]\u200b` instead of looping."),
    ("```suggestion\nuser = data['users'][0]\n```",
     "```suggestion\nuser = data['users']\u200b[0]\u200b\n```"),
    ("Check that a < b before dividing.", "Check that a <\u200b b before dividing."),
    # as 0.1.0 posted it
    ("Ask @alice if `rows[0]` can be empty, see #12.",
     "Ask @\u200balice if `rows[0]` can be empty, see #\u200b12."),
])
def test_comment_an_earlier_version_posted_counts_as_posted(github, description, as_posted):
    github.pr.get_reviews.return_value = [MagicMock(body=f"old\n\n{MARKER}")]
    github.pr.get_review_comments.return_value = [posted_comment(description=as_posted)]

    GithubClient(token="t").post_review(
        make_pr_summary(), "summary", [(finding(description=description), 3)]
    )

    github.pr.create_review.assert_not_called()

def test_later_versions_still_find_the_comment_0_1_0_posted(github, monkeypatch):
    # say a later version breaks every "]" again, in code too
    neutralise = client_module.neutralise
    monkeypatch.setattr(client_module, "neutralise",
                        lambda text: neutralise(text).replace("]", "]\u200b"))
    github.pr.get_reviews.return_value = [MagicMock(body=f"old\n\n{MARKER}")]
    github.pr.get_review_comments.return_value = [
        posted_comment(description="Ask @\u200balice if `rows[0]` can be empty, see #\u200b12.")
    ]

    GithubClient(token="t").post_review(
        make_pr_summary(), "summary",
        [(finding(description="Ask @alice if `rows[0]` can be empty, see #12."), 3)]
    )

    github.pr.create_review.assert_not_called()

def test_line_endings_and_spaces_at_line_ends_do_not_make_a_comment_new(github):
    # as 0.1.0 posted a description with \r\n line endings and spaces at the end of a line: the
    # same text without them says the same
    github.pr.get_reviews.return_value = [MagicMock(body=f"old\n\n{MARKER}")]
    github.pr.get_review_comments.return_value = [
        posted_comment(description="First this.  \r\nThen that.\r\n")
    ]

    GithubClient(token="t").post_review(
        make_pr_summary(), "summary", [(finding(description="First this.\nThen that."), 3)]
    )

    github.pr.create_review.assert_not_called()

# a suggestion that differs in anything else is another suggestion, and gets a comment of its own
@pytest.mark.parametrize("as_posted", [
    "**HIGH** — Risky call\n\n```suggestion\n    return rows[1]\n```",     # other code
    "**HIGH** — Risky call\n\n```suggestion\nreturn rows[0]\n```",         # other indentation
    "**HIGH** — Risky call\n\n```suggestion\n    return  rows[0]\n```",    # other spacing
    "**HIGH** — Risky call\n\n```suggestion\n    return rows[0]\n\n```",   # another line
    "**HIGH** — Risky call\n\n```suggestion\n    return\n    rows[0]\n```",   # other lines
    "**MEDIUM** — Risky call\n\n```suggestion\n    return rows[0]\n```",   # another severity
])
def test_a_comment_that_says_something_else_does_not_count_as_posted(github, as_posted):
    github.pr.get_reviews.return_value = [MagicMock(body=f"old\n\n{MARKER}")]
    github.pr.get_review_comments.return_value = [
        SimpleNamespace(path="app.py", line=3, body=as_posted)
    ]
    suggestion = finding(description="```suggestion\n    return rows[0]\n```")

    GithubClient(token="t").post_review(make_pr_summary(), "summary", [(suggestion, 3)])

    assert [c["line"] for c in github.pr.create_review.call_args.kwargs["comments"]] == [3]
