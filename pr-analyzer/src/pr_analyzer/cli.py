import typer
from typing import Annotated
from logging_config import setup_logging
from pipeline import AnalysisPipeline
from exceptions import (
    InvalidTokenError,
    PrivateRepoError,
    PRNotFoundError,
    RateLimitError,
    RepositoryNotFoundError,
    PRAnalyzerError,
)
from reporting.rich_reporter import (
    print_rich_report,
    print_json_report,
    print_markdown_report,
)

# creates the Typer app — this is what powers the CLI
# add_completion=False removes the shell autocomplete install prompt
app = typer.Typer(add_completion=False)


@app.command()
def analyze(
    pr_url: Annotated[str, typer.Argument(help="Full GitHub PR URL")],
    skip_llm: Annotated[bool, typer.Option("--skip-llm", help="Skip LLM call, run static and security only")] = False,
    post_review: Annotated[bool, typer.Option("--post-review", help="Post results as inline GitHub review comments")] = False,
    fail_on_block: Annotated[bool, typer.Option("--fail-on-block", help="Exit with code 1 if blocking issues found")] = True,
    output: Annotated[str, typer.Option("--output", help="Output format: rich, json, markdown")] = "rich",
):
    """
    Analyze a GitHub pull request for code quality issues.

    Usage:
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766 --skip-llm
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766 --output json
    """

    # start logging before anything else runs
    setup_logging()

    # run the pipeline — this is the only business logic cli.py triggers
    # everything else is just error handling and output routing
    try:
        pipeline = AnalysisPipeline(skip_llm=skip_llm, post_review=post_review)
        result = pipeline.run(pr_url)

    except ValueError as e:
        # bad URL format — user typed the wrong kind of URL
        typer.echo(f"Invalid URL: {e}", err=True)
        raise typer.Exit(code=2)

    except InvalidTokenError as e:
        # token is wrong or missing from .env
        typer.echo(f"Authentication failed: {e}", err=True)
        typer.echo("Check your GITHUB_TOKEN in .env", err=True)
        raise typer.Exit(code=2)

    except PrivateRepoError as e:
        # token works but does not have permission for this repo
        typer.echo(f"Access denied: {e}", err=True)
        typer.echo("Your token needs the 'repo' scope for private repositories", err=True)
        raise typer.Exit(code=2)

    except PRNotFoundError as e:
        # repo is fine, token is fine, but the PR number does not exist
        typer.echo(f"PR not found: {e}", err=True)
        raise typer.Exit(code=2)

    except RepositoryNotFoundError as e:
        # the repo itself does not exist — typo in owner or repo name
        typer.echo(f"Repository not found: {e}", err=True)
        raise typer.Exit(code=2)

    except RateLimitError as e:
        # too many GitHub API calls — e.reset_time tells user when to retry
        typer.echo(f"GitHub rate limit hit. Try again after {e.reset_time.strftime('%H:%M:%S')}", err=True)
        raise typer.Exit(code=2)

    except PRAnalyzerError as e:
        # catch-all for any other known error from the pipeline
        typer.echo(f"Analysis failed: {e}", err=True)
        raise typer.Exit(code=2)

    # hand off to the correct reporter — cli.py never prints anything itself
    # rich is the default, json and markdown are for CI and sharing
    if output == "json":
        print_json_report(result)
    elif output == "markdown":
        print_markdown_report(result)
    else:
        print_rich_report(result)

    # exit code is the only thing cli.py "returns" to the terminal
    # code 1 means blocked — CI systems use this to fail the pipeline
    # code 0 means passed — everything is fine
    if fail_on_block and not result.passed:
        raise typer.Exit(code=1)

    raise typer.Exit(code=0)


@app.command()
def version():
    """Show the current version of pr-analyzer."""
    typer.echo("pr-analyzer 0.1.0")
    typer.echo("Static analysis · Security scanning · LLM suggestions")


if __name__ == "__main__":
    app()