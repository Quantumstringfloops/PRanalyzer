from urllib.parse import urlsplit, urlparse

def parse_url_and_validate(pr_url):
    if pr_url == None or "":
        return "Invalid input: Empty string"
    
    o = urlsplit(pr_url)
    try:
        if o.netloc in ["www.github.com", "github.com"] & o.scheme == "https":
            path_parts = o.path.strip("/").split("/")
            # checks if repo url is valid
            if len(path_parts) != 4:
                return "Invalid PR url"
            repo_name = path_parts[2]
            pr_number = path_parts[3]
            if type(pr_number) is not int:
                return {"valid": False, "Error": "Invalid PR number in the url"}

            return {
                "valid":True,
                "repo_name": repo_name,
                "pr_number": pr_number
            }
            

    except Exception :
        pass

    return {"valid": False, "error": "Invalid GitHub repository URL"}


result = parse_url_and_validate("https://github.com/octocat/Hello-World/pull/42")
print(result) 