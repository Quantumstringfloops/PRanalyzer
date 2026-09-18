from github import Github, Auth

auth = Auth.token("")
g = Github(auth = auth)

repo = Github.get_repo("microsoft/vscode")