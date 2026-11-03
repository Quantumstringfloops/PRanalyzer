import importlib.metadata
import traceback
from enum import Enum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pr_analyzer.config import settings
from pr_analyzer.exceptions import (
    ConfigError,
    InvalidPRURLError,
    InvalidTokenError,
    PrivateRepoError,
    PRNotFoundError,
    RateLimitError,
    RepositoryNotFoundError,
    PRAnalyzerError,
)
from pr_analyzer.logging_config import setup_logging
from pr_analyzer.models import AnalysisResult
from pr_analyzer.pipeline import AnalysisPipeline
from pr_analyzer.project_config import load_project_config
from pr_analyzer.reporting.rich_reporter import RichReporter

# exit codes: 0 = passed, 1 = blocked (with --fail-on-block), 2 = the PR could not be analysed
EXIT_BLOCKED = 1
EXIT_ERROR = 2


class OutputFormat(str, Enum):
    rich = "rich"
    json = "json"
    markdown = "markdown"


# creates the Typer app — this is what powers the CLI
# add_completion=False removes the shell autocomplete install prompt
app = typer.Typer(add_completion=False)
reporter = RichReporter()


def fail(*messages: str) -> NoReturn:
    for message in messages:
        typer.echo(message, err=True)
    raise typer.Exit(code=EXIT_ERROR)


# the name on PyPI; the command and the import package are still called pr-analyzer / pr_analyzer
DISTRIBUTION = "pr-analyzer-cli"


def package_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def error_messages(e: PRAnalyzerError) -> tuple[str, ...]:
    """What to tell the user about an error the pipeline is expected to raise."""
    if isinstance(e, InvalidPRURLError):
        # bad URL format — user typed the wrong kind of URL
        return (f"Invalid URL: {e}",)
    if isinstance(e, ConfigError):
        # the --config file can't be read or is invalid; e lists every problem. An invalid
        # .pr-analyzer.toml on the base branch only gets a warning (see pipeline.py)
        return (f"Configuration error: {e}",)
    if isinstance(e, InvalidTokenError):
        # token is wrong, expired or missing
        return (f"Authentication failed: {e}", "Check GITHUB_TOKEN in your .env or environment")
    if isinstance(e, PrivateRepoError):
        # token works but does not have permission for this repo
        return (
            f"Access denied: {e}",
            "Classic tokens need the 'repo' scope for private repositories; fine-grained "
            "tokens need read access to Contents and Pull requests",
        )
    if isinstance(e, PRNotFoundError):
        # repo is fine, token is fine, but the PR number does not exist
        return (f"PR not found: {e}",)
    if isinstance(e, RepositoryNotFoundError):
        # the repo does not exist (typo in owner or repo name) or the token can't see it
        return (f"Repository not found: {e}",)
    if isinstance(e, RateLimitError):
        # too many GitHub API calls — e.reset_time tells user when to retry
        return (f"GitHub rate limit hit. Try again after {e.reset_time.strftime('%H:%M:%S')}",)
    # catch-all for any other known error from the pipeline
    return (f"Analysis failed: {e}",)


def print_report(result: AnalysisResult, output: OutputFormat) -> None:
    # rich is the default, json and markdown are for CI and sharing
    if output is OutputFormat.json:
        reporter.print_json_report(result)
    elif output is OutputFormat.markdown:
        reporter.print_markdown_report(result)
    else:
        reporter.print_rich_report(result)


@app.command()
def analyze(
    pr_url: Annotated[str, typer.Argument(help="Full GitHub PR URL")],
    skip_llm: Annotated[bool, typer.Option("--skip-llm", help="Skip the LLM review, run static and security checks only")] = False,
    post_review: Annotated[bool, typer.Option("--post-review", help="Post results as a GitHub review with inline comments")] = False,
    fail_on_block: Annotated[bool, typer.Option("--fail-on-block/--no-fail-on-block", help="Exit with code 1 if blocking issues are found")] = True,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Report format")] = OutputFormat.rich,
    config: Annotated[Path | None, typer.Option(
        "--config", help="Settings file to use instead of .pr-analyzer.toml on the PR's base branch",
    )] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show progress logs (on stderr)")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Don't read or write the on-disk cache")] = False,
):
    """
    Analyze a GitHub pull request for code quality issues.

    Usage:
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766 --skip-llm
        pr-analyzer analyze https://github.com/microsoft/vscode/pull/336766 --output json
    """

    # start logging before anything else runs; logs go to stderr so stdout holds only the report
    setup_logging("INFO" if verbose else settings.log_level)

    # run the pipeline — this is the only business logic cli.py triggers
    # everything else is just error handling and output routing
    pipeline = None
    try:
        # a bad --config file stops the run here, before any GitHub request. Without --config the
        # pipeline reads .pr-analyzer.toml from the PR's base branch, never from the current
        # directory: in CI that is the PR's own checkout, which could loosen its own checks.
        project_config = load_project_config(config) if config is not None else None
        pipeline = AnalysisPipeline(skip_llm=skip_llm, post_review=post_review, use_cache=not no_cache)
        if project_config is not None:
            pipeline.project_config = project_config
        result = pipeline.run(pr_url)

    except PRAnalyzerError as e:
        fail(*error_messages(e))

    except Exception as e:
        # a bug, not a verdict: exit 2 so CI can tell it apart from a blocked PR (exit 1)
        if verbose:
            typer.echo(traceback.format_exc(), err=True)
        fail(f"Unexpected error: {e!r}", "Re-run with --verbose to see the full traceback")

    finally:
        if pipeline is not None:
            pipeline.close()

    # hand off to the correct reporter — cli.py never prints anything itself
    print_report(result, output)

    # exit code is the only thing cli.py "returns" to the terminal
    # code 1 means blocked — CI systems use this to fail the pipeline
    # code 0 means passed — everything is fine
    if fail_on_block and not result.passed:
        raise typer.Exit(code=EXIT_BLOCKED)


@app.command()
def version():
    """Show the current version of pr-analyzer."""
    typer.echo(f"pr-analyzer {package_version()}")
    typer.echo("Static analysis · Security scanning · LLM suggestions")


if __name__ == "__main__":
    app()
