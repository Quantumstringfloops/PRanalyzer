import argparse
import github
from github import GithubException

from urllib.parse import urlsplit, urlparse

def parse_url_and_validate(pr_url):
    if pr_url == None or "":
        return "Invalid input: Empty string"
    
    o = urlsplit(pr_url)

    print("netloc:",o.netloc)
    print("url:",o.geturl)
    print("urlsploit:",o)
    print("urlpath:",o.path)
    print("scheme:",o.scheme)
    print("query:",o.query)


parse_url_and_validate("https://github.com/octocat/Hello-World/pull/42")
    