from github import GithubException
import github
import re
from urllib.parse import urlparse, urlsplit


def parse_url(repo_url):
    # 1. Handle SSH shorthand format (git@github.com:owner/repo.git)
    ssh_pattern = r"^git@github\.com:(?P<owner>[a-zA-Z0-9_.-]+)/(?P<repo>[a-zA-Z0-9_.-]+?)(?:\.git)?$"
    o = urlsplit(repo_url)

    pattern = r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pr_number>\d+)/?"
    
    match = re.match(pattern, repo_url)
    if match:
        data = match.groupdict()
        return {
            "valid": True,
            "owner": data["owner"],
            "repo": data["repo"],
            "pr_number": int(data["pr_number"]),
            "huh": o  # Converted to integer for easy use
        }
    
    

    return {"valid": False, "error": "Invalid GitHub repository URL", "huh": o}