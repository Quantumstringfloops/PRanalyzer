from urllib.parse import urlsplit, urlparse
from github import Github, Auth
from github import GithubException
from exceptions import (
    InvalidTokenError,
    PrivateRepoError,
    PRNotFoundError,
    RateLimitError,
    RepositoryNotFoundError,
    PRAnalyzerError
)
from dotenv import load_dotenv
import os
load_dotenv()
class GithubClient:

    def __init__(self):
        token = os.getenv("GITHUB_ACCESS_TOKEN")
        auth = Auth.Token(token)
        self.git_instance = Github(auth=auth)


    def fetch_pr(self,url: str) -> PRSummary:
        repo_name, pr_number = self.parse_url_and_validate(url)

        try: 
            repo = self.git_instance.get_repo(repo_name)
            pull_request = repo.get_pull(pr_number)
        except GithubException as e:
            if e.status == 401:
                raise InvalidTokenError("Token is missing or invalid") from e
            elif e.status == 403:
                raise PrivateRepoError(f"{repo_name} is private or token lacks permission.") from e
            elif e.status == 404:
                raise PRNotFoundError(f"PR #{pr_number} not found in {repo_name}.") from e
            else:
                raise PRAnalyzerError(f"GitHub error {e.status}: {e.data}") from e

        finally:
            self.git_instance.close()


    def parse_url_and_validate(self, pr_url: str) -> tuple[str, int]:
        if pr_url == None or "":
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
            raise ValueError(f"PR number must be an integer, got: {parts[3]!r}")
        repo_name = path_parts[0]+"/"+path_parts[1]
        pr_number = path_parts[3]
            
        return (
            repo_name,
            pr_number
            )
    