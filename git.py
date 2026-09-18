import argparse
import os
from github import Github
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
        print(f"repo name :", repo.name)
        print(f"repo desc: ", repo.description)


        pull_requests = repo.get_pulls(
            state="open",
            sort="updated",
            direction="desc"
        )

        for index, pr in enumerate(pull_requests):
            if index == 10:
                break

            author = pr.user.login
            files_changed = pr.changed_files
            title = pr.title

            print(f" Pull request details: author: {author}, title: {title}, files_changed: {files_changed}")

    except GithubException as error:
        print(f"GitHub API error: {error.status}")
        print(error.data)
        return None

    finally:
        github.close()


connect_to_github('microsoft/vscode')
