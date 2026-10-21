import argparse
import os
from github import Github
from github.GithubException import GithubException
from regex import find_all_todos, find_cl
from urlvalidator import parse_url

# def analyze_patch(patch):
#     added_lines = []
#     removed_lines = []

#     if not patch : 
#         return added_lines, removed_lines

#     for line in patch.splitlines():
#         if line.startswith("+++"):
#             continue
#         if line.startswith("---"):
#             continue


#         if line.startswith("+"):
#             added_lines.append(line[1:])
#         elif line.startswith("-"): 
#             removed_lines.append(line[1:])


#     return added_lines,removed_lines

def connect_to_github(repository_name:str):
    # Only for private repos
    # token = os.getenv("GITHUB_TOKEN")
    # if token: 
    #     return Github(auth = Auth.token(token)) 

    github = Github()

    try: 
        repo = github.get_repo(repository_name)
        print("repo: ", repo)
        print("Connection succesful \n")
        print(f"repo name :", repo.name)
        print(f"repo desc: ", repo.description)


        pull_requests = repo.get_pulls(
            state="open",
            sort="updated",
            direction="desc"
        )

        for index, pr in enumerate(pull_requests):
            if index == 5: # first 5
                break

            author = pr.user.login
            files_changed = pr.changed_files
            title = pr.title
            number = pr.number

            print(f" Pull request details: author: {author}, title: {title},number: {number}, files_changed: {files_changed}")

        pullr = repo.get_pull(341147)
        print("FOCUS OVER HERE",pullr)
        for changed_file in pullr.get_files():
            print("File:", changed_file.filename)
            fext = os.path.splitext(changed_file.filename)[1]
            print("File list: ",fext)
            print("Status:", changed_file.status)
            print("Additions:", changed_file.additions)
            print("Deletions:", changed_file.deletions)
            print("Patch: ", changed_file.patch)
            # print("\nPatch:")

            # added ,removed = analyze_patch(changed_file.patch)

            # print("Added: ")
            # for line in added:
            #     print("+", line)

            # print("Removed: ")
            # for line in removed:
            #     print("-", line)

            break # prints only the first changed file

    except GithubException as error:
        print(f"GitHub API error: {error.status}")
        print(error.data)
        return None

    finally:
        github.close()


connect_to_github('microsoft/vscode')

