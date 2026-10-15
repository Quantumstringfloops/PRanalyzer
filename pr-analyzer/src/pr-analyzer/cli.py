import argparse
import github
from github import GithubException

from urllib.parse import urlsplit, urlparse

def parse_url_and_validate(pr_url):
    if pr_url == None or "":
        return "Invalid input: Empty string"
    
    o = urlsplit(pr_url)
    try:
        if o.netloc in ["www.github.com", "github.com"]:
            o = urlsplit(pr_url) 
            path_parts = o.path.strip("/").split("/")
            # checks if repo url is valid
            if len(path_parts) != 4:
                return "Invalid PR url"
            pr_number = path_parts[3]
            repo_name = path_parts[2]
            print(path_parts)

    except Exception :
        pass

    return {"valid": False, "error": "Invalid GitHub repository URL"}


parse_url_and_validate("https://github.com/octocat/Hello-World/pull/42")
    