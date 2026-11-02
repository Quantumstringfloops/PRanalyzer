from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
import structlog
from github import Auth, Github, GithubException, RateLimitExceededException
from urllib3.util.retry import Retry

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
from pr_analyzer.models import FileChange, PRSummary, Suggestion

log = structlog.get_logger(__name__)

GITHUB_HOSTS = {"github.com", "www.github.com"}
# PR pages people copy URLs from: /pull/123, /pull/123/files, /pull/123/commits, /pull/123/checks
PR_SUBPAGES = {"files", "commits", "checks"}


class GithubClient:

    def __init__(self, token: str | None = None):
        self.token = settings.github_token if token is None else token
        # retry transient 5xx errors on reads; rate-limit 403s are not retried but surface as
        # RateLimitError, so the user learns when to try again instead of the CLI sleeping for it
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(500, 502, 503, 504),
            raise_on_status=False,
        )
        auth = Auth.Token(self.token) if self.token else None
        self.git_instance = Github(auth=auth, timeout=settings.github_timeout_seconds, retry=retry)

    def close(self) -> None:
        self.git_instance.close()

    def fetch_pr(self, url: str) -> PRSummary:
        repo_name, pr_number = self.parse_url_and_validate(url)
        if not self.token:
            log.warning("github_token_missing", hint="unauthenticated requests are limited to 60/hour")
        log.info("fetch_started", repo=repo_name, pr=pr_number)

        try:
            try:
                repo = self.git_instance.get_repo(repo_name)
            except GithubException as e:
                if e.status == 404:
                    raise RepositoryNotFoundError(
                        f"{repo_name} does not exist or your token cannot see it"
                    ) from e
                raise

            try:
                pr = repo.get_pull(pr_number)
            except GithubException as e:
                if e.status == 404:
                    raise PRNotFoundError(f"PR #{pr_number} not found in {repo_name}") from e
                raise

            return PRSummary(
                number=pr.number,
                title=pr.title,
                author=pr.user.login if pr.user else "unknown",
                repo=repo_name,
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                total_additions=pr.additions,
                total_deletions=pr.deletions,
                is_draft=bool(pr.draft),
                files=self.build_file_changes(pr, repo),
                head_sha=pr.head.sha,
            )
        except RateLimitExceededException as e:
            raise RateLimitError(self._reset_time(e)) from e
        except GithubException as e:
            raise self._map_error(e, repo_name) from e
        except requests.exceptions.RequestException as e:
            raise PRAnalyzerError(f"Could not reach GitHub: {e}") from e

    def parse_url_and_validate(self, pr_url: str) -> tuple[str, int]:
        if not pr_url or not pr_url.strip():
            raise InvalidPRURLError("The URL cannot be empty")

        parts = urlsplit(pr_url.strip())
        if parts.scheme != "https":
            raise InvalidPRURLError("URL must start with https://")
        if parts.netloc.lower() not in GITHUB_HOSTS:
            raise InvalidPRURLError("Domain must be github.com")

        path = parts.path.strip("/").split("/")
        if len(path) == 5 and path[4] in PR_SUBPAGES:
            path = path[:4]
        if len(path) != 4 or path[2] != "pull" or not path[0] or not path[1]:
            raise InvalidPRURLError("Expected https://github.com/<owner>/<repo>/pull/<number>")
        if not (path[3].isascii() and path[3].isdigit()):
            raise InvalidPRURLError(f"PR number must be an integer, got: {path[3]!r}")

        repo_name = f"{path[0]}/{path[1]}"
        pr_number = int(path[3])
        log.info("url_validated", repo=repo_name, pr=pr_number)
        return repo_name, pr_number

    def build_file_changes(self, pr, repo) -> list[FileChange]:
        files = []
        for f in pr.get_files():
            if f.patch is None:
                # binary files, very large diffs and pure renames come without a patch
                log.info("file_skipped_no_patch", filename=f.filename, status=f.status)
                continue

            raw_content = None
            if f.status != "removed":
                raw_content = self._fetch_content(repo, f.filename, pr.head.sha)

            files.append(FileChange(
                filename=f.filename,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
                patch=f.patch,
                raw_content=raw_content,
            ))

        log.info("files_fetched", repo=repo.full_name, pr=pr.number, file_count=len(files))
        return files

    def _fetch_content(self, repo, path: str, ref: str) -> str | None:
        try:
            content = repo.get_contents(path, ref=ref)
        except RateLimitExceededException:
            raise
        except GithubException as e:
            log.warning("content_unavailable", filename=path, status=e.status)
            return None

        # a list means a directory or submodule; files over 1 MB come back without base64 content
        if isinstance(content, list) or content.encoding != "base64":
            log.warning("content_unavailable", filename=path, reason="not a file or over 1 MB")
            return None
        return content.decoded_content.decode("utf-8", errors="replace")

    def post_review(
        self, pr: PRSummary, body: str, comments: list[tuple[Suggestion, int]]
    ) -> None:
        if not self.token:
            raise InvalidTokenError("--post-review needs a GITHUB_TOKEN that can write pull requests")

        review_comments = [
            {
                "path": suggestion.filename,
                "line": line,
                "side": "RIGHT",
                "body": f"**{suggestion.severity}** — {suggestion.title}\n\n{suggestion.description}",
            }
            for suggestion, line in comments
        ]

        try:
            repo = self.git_instance.get_repo(pr.repo)
            pull = repo.get_pull(pr.number)
            # pin the review to the analysed commit so line numbers match even if the PR moved on
            extra = {"commit": repo.get_commit(pr.head_sha)} if pr.head_sha else {}
            try:
                pull.create_review(body=body, event="COMMENT", comments=review_comments, **extra)
            except GithubException as e:
                if e.status != 422 or not review_comments:
                    raise
                # GitHub rejects the whole review if any inline comment falls outside the diff;
                # post the summary alone rather than nothing
                log.warning("inline_comments_rejected", error=str(e.data))
                pull.create_review(
                    body=body + "\n\n_Inline comments could not be attached to this diff._",
                    event="COMMENT",
                    **extra,
                )
                review_comments = []
        except RateLimitExceededException as e:
            raise RateLimitError(self._reset_time(e)) from e
        except GithubException as e:
            raise self._map_error(e, pr.repo) from e
        except requests.exceptions.RequestException as e:
            raise PRAnalyzerError(f"Could not reach GitHub: {e}") from e

        log.info("review_posted", pr=pr.number, comments=len(review_comments))

    @staticmethod
    def _map_error(e: GithubException, repo_name: str) -> PRAnalyzerError:
        if e.status == 401:
            return InvalidTokenError("GITHUB_TOKEN is invalid or expired")
        if e.status == 403:
            return PrivateRepoError(f"Your token is not allowed to access {repo_name}")
        message = e.data.get("message") if isinstance(e.data, dict) else e.data
        return PRAnalyzerError(f"GitHub error {e.status}: {message}")

    @staticmethod
    def _reset_time(e: RateLimitExceededException) -> datetime:
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            if "retry-after" in headers:  # secondary rate limits say how long to wait
                return datetime.now() + timedelta(seconds=int(headers["retry-after"]))
            return datetime.fromtimestamp(int(headers["x-ratelimit-reset"]))
        except (KeyError, TypeError, ValueError):
            return datetime.now()
