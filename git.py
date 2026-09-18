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


        # pull_requests = repo.get_pulls(
        #     state="open",
        #     sort="updated",
        #     direction="desc"
        # )

        # for index, pr in enumerate(pull_requests):
        #     if index == 5: # first 5
        #         break

        #     author = pr.user.login
        #     files_changed = pr.changed_files
        #     title = pr.title
        #     number = pr.number

        #     print(f" Pull request details: author: {author}, title: {title},number: {number}, files_changed: {files_changed}")

        pullr = repo.get_pull(336766)
        for changed_file in pullr.get_files():
            print("File:", changed_file.filename)
            print("Status:", changed_file.status)
            print("Additions:", changed_file.additions)
            print("Deletions:", changed_file.deletions)
            print("\nPatch:")

            print(changed_file.patch)
            break # prints only the first changed file

    except GithubException as error:
        print(f"GitHub API error: {error.status}")
        print(error.data)
        return None

    finally:
        github.close()


connect_to_github('microsoft/vscode')
