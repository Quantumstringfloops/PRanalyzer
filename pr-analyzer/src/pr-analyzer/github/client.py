from urllib.parse import urlsplit, urlparse
from github import Github, Auth
from github import GithubException
from exceptions import (
    InvalidTokenError,
    PrivateRepoError,
    PRNotFoundError,
    RateLimitError,
    RepositoryNotFoundError
)
from dotenv import load_dotenv
import os
load_dotenv()
class GithubClient:

    def __init__(self):
        token = os.getenv("GITHUB_ACCESS_TOKEN")
        auth = Auth.Token(token)
        g= Github(auth=auth)


    def fetch_pr(self,url: str) -> PRSummary:
        repo_name, pr_number = self.parse_url_and_validate(url)

        try: 
            pull_request = g.


    def parse_url_and_validate(self, pr_url: str) -> tuple[str, int]:
        if pr_url == None or "":
            return "Invalid input: Empty string"
        
        o = urlsplit(pr_url)
        try:
            if o.netloc in ["www.github.com", "github.com"] and o.scheme == "https":
                path_parts = o.path.strip("/").split("/")
                # checks if repo url is valid
                if len(path_parts) != 4:
                    return "Invalid PR url"
                repo_name = path_parts[0]+"/"+path_parts[1]
                try: 
                    pr_number = int(path_parts[3])
                except ValueError:
                    return {"valid": False, "Error": "Invalid PR number in the url"}
                return (
                    repo_name,
                    pr_number
                )
                

        except Exception as e:
            return e

    


result = parse_url_and_validate("https://github.com/octocat/Hello-World/pull/42")
print(result)