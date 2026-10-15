import argparse
import github
from github import GithubException

from urllib.parse import urlsplit, urlparse

def parse_url_and_validate(pr_url):
    o = urlsplit(pr_url)
    print(o)