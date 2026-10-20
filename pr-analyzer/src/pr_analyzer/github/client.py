from urllib.parse import urlsplit, urlparse
from github import Github, Auth
from github import GithubException, RateLimitExceededException, UnknownObjectException
from datetime import datetime
from pr_analyzer.exceptions import (
    InvalidTokenError,
    PrivateRepoError,
    PRNotFoundError,
    RateLimitError,
    PRAnalyzerError
)
from pr_analyzer.config import settings
from pr_analyzer.models import PRSummary, FileChange
import structlog

log = structlog.get_logger(__name__)

class GithubClient:

    def __init__(self):
        token = settings.github_token
        auth = Auth.Token(token)
        self.git_instance = Github(auth=auth)


    def fetch_pr(self,url: str) -> PRSummary:
        repo_name, pr_number = self.parse_url_and_validate(url)
        log.info("fetch_started", url=url)
        try: 
            repo = self.git_instance.get_repo(repo_name)
            pr = repo.get_pull(pr_number)

            return PRSummary(
                number=pr.number,
                title=pr.title,
                author=pr.user.login,
                repo=repo_name,
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                total_additions=pr.additions,
                total_deletions=pr.deletions,
                is_draft=pr.draft,
                files=self.build_file_changes(pr, repo)
            )
        except RateLimitExceededException as e:
            reset_time = datetime.fromtimestamp(self.git_instance.rate_limiting_resettime)
            raise RateLimitError(reset_time) from e
        
        except GithubException as e:
            if e.status == 401:
                log.warning("Invalid or missing token")
                raise InvalidTokenError("Token is missing or invalid") from e
            elif e.status == 403:
                log.warning("Repository is private or token lacks permission", repo_name = repo_name)
                raise PrivateRepoError(f"{repo_name} is private or token lacks permission.") from e
            elif e.status == 404:
                log.warning("PR number not found", pr=pr_number, repo_name=repo_name)
                raise PRNotFoundError(f"PR #{pr_number} not found in {repo_name}.") from e
            else:
                log.warning("Github error")
                raise PRAnalyzerError(f"GitHub error {e.status}: {e.data}") from e

        finally:
            self.git_instance.close()


    def parse_url_and_validate(self, pr_url: str) -> tuple[str, int]:
        if not pr_url:
            raise ValueError("The URL string cannot be empty")
        
        o = urlsplit(pr_url)
        if o.scheme != "https":
            raise ValueError("URL must be https")
        if o.netloc not in ["www.github.com", "github.com"]:
            raise ValueError("Domain must be github.com") 
        path_parts = o.path.strip("/").split("/")
            # checks if repo url is valid
        if len(path_parts) != 4 or path_parts[2] != "pull":
                raise ValueError("Invalid Pull request URL")
        if not path_parts[3].isdigit():
            raise ValueError(f"PR number must be an integer, got: {path_parts[3]!r}")
        
        repo_name = path_parts[0]+"/"+path_parts[1]
        pr_number = int(path_parts[3])
        log.info("url_validated", repo=repo_name, pr=pr_number)
            
        return (
            repo_name,
            pr_number
            )

    
    def build_file_changes(self, pr, repo) -> list[FileChange]:
        files = []
        max_diif_lines = settings.max_diff_lines
        log.info("Fetching files ", repo=repo.name, pr=pr.number)
        for f in pr.get_files():
            raw_content = None
            if f.patch is None:
                log.warning("Skipped binary file", filename=f.filename)
                continue
            lines = f.patch.splitlines()
            if len(lines) > max_diif_lines:
                log.info("Max lines exceeded", max_lines = max_diif_lines)
                lines = lines[:max_diif_lines]
                lines.append[("....truncated")]
                log.info("patch_truncated", filename=f.filename, original_lines=len(lines), kept=max_diif_lines)
            patch = "\n".join(lines)
            if f.status != "removed":
                try:
                    content_file = repo.get_contents(f.filename, ref =pr.head.sha)
                    raw_content = content_file.decoded_content.decode("utf-8", errors="replace")
                except UnknownObjectException:
                    log.warning("Unkown object error")
                    raw_content = None
            files.append(FileChange(
                filename= f.filename,
                status= f.status,
                additions= f.additions,
                deletions= f.deletions,
                patch = patch,
                raw_content= raw_content
            )) 
        log.info("Fetching of files was succesful", repo=repo.name, pr=pr.number, file_count = pr.changed_files)
        return files