import argparse
import os
from github import Github, Auth
from github.GithubException import GithubException


def connect_to_github(repository_name:str):
    # Only for private repos
    # token = os.getenv("GITHUB_TOKEN")
    # if token: 
    #     return Github(auth = Auth.token(token)) 

    github = Github()

    try: 
        repo = github.get_repo(repository_name)

        print("Connection succesful \n")
        print(f"repo name : {repo._full_name}")
        print(f"repo desc: {repo.description}")

    except GithubException as error:
        print(f"GitHub API error: {error.status}")
        print(error.data)
        return None

    finally:
        github.close()


connect_to_github('microsoft/vscode')
