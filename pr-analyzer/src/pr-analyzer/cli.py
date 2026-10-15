import argparse
import github
from github import GithubException

from urllib.parse import urlsplit, urlparse

def parse_url_and_validate(pr_url):
    if pr_url == None or "":
        return "Invalid input: Empty string"
    
    o = urlsplit(pr_url)
    