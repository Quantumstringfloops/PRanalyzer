from github import GithubException
import github
import re
from urllib.parse import urlparse 


def parse_url(repo_url):
    # 1. Handle SSH shorthand format (git@github.com:owner/repo.git)
    ssh_pattern = r"^git@github\.com:(?P<owner>[a-zA-Z0-9_.-]+)/(?P<repo>[a-zA-Z0-9_.-]+?)(?:\.git)?$"

    pattern = r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pr_number>\d+)/?"
    
    match = re.match(pattern, url)
    if match:
        data = match.groupdict()
        return {
            "valid": True,
            "owner": data["owner"],
            "repo": data["repo"],
            "pr_number": int(data["pr_number"])  # Converted to integer for easy use
        }
    
    try:
        parsed = urlparse(repo_url)
        if parsed.netloc in ["github.com", "www.github.com"]:
            path_parts = parsed.path.strip("/").split("/")
            if len(path_parts) >= 2:
                owner = path_parts[0]
                repo = path_parts[1].removesuffix(".git")
                return {
                    "valid": True,
                    "type": "https",
                    "owner": owner,
                    "repo": repo
                }
    except Exception:
        pass

    return {"valid": False, "error": "Invalid GitHub repository URL"}