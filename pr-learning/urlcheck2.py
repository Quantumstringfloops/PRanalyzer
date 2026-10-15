from urllib.parse import urlsplit
def parse_url_and_validate(pr_url: str) -> tuple[str, int]:
        if pr_url == None or "":
            raise ValueError("The URL string cannot be empty")
        
        o = urlsplit(pr_url)
        if o.scheme != "https":
            raise ValueError("URL must be https")
        if o.netloc not in ["www.github.com", "github.com"]:
            raise ValueError("Domain must be github.com") 
        path_parts = o.path.strip("/").split("/")
            # checks if repo url is valid
        if len(path_parts) != 4 or path_parts[2] != "pull":
                raise ValueError("Invalid Pull request URL")
        if not path_parts[3].isdigit():
            raise ValueError(f"PR number must be an integer, got: {path_parts[3]!r}")
        repo_name = path_parts[0]+"/"+path_parts[1]
        pr_number = path_parts[3]
            
        return (
            repo_name,
            pr_number
            )

    


result = parse_url_and_validate("https://github.com/microsoft/vscode/pull/341850a")
print(result)