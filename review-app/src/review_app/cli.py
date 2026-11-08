"""review-app's commands: the webhook server, the dashboard, a standalone worker, and one-off
checks. Run them from the folder that holds .env and review-app.toml."""
import ipaddress
import json
import re
import threading
from collections.abc import Callable
from functools import partial
from typing import Annotated, NoReturn

import typer
import uvicorn
from pr_analyzer.config import settings as analyzer_settings
from pr_analyzer.llm.engine import DEFAULT_MODELS
from pr_analyzer.logging_config import setup_logging
from pr_analyzer.security import tools

from review_app.analysis import SCANNERS, PrAnalyzer
from review_app.db import Store
from review_app.github_app import AppAccess, InstallationTokens, TokenAccess
from review_app.jobs import MAX_ATTEMPTS, Job, JobQueue
from review_app.policy import REPO_NAME, PolicyBook, PolicyError, load_policy
from review_app.settings import AppSettings, SettingsError, use_nvidia_by_default
from review_app.web import create_dashboard_app, create_webhook_app
from review_app.webhooks import WebhookHandler
from review_app.worker import Worker

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Review pull requests with pr-analyzer, then merge, escalate or block them.")
PR_URL = re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)/?$")


def fail(*lines: str) -> NoReturn:
    for line in lines:
        typer.echo(line, err=True)
    raise typer.Exit(code=1)


def load() -> tuple[AppSettings, PolicyBook, Store]:
    settings = AppSettings()
    use_nvidia_by_default(analyzer_settings)
    try:
        policies = load_policy(settings.review_app_policy)
    except PolicyError as e:
        fail(str(e))   # fail closed: never run on a policy the operator didn't mean
    store = Store(settings.review_app_db)
    store.init()
    return settings, policies, store


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def environment_health(settings: AppSettings, policies: PolicyBook) -> list[tuple[str, bool, str]]:
    app_problems = settings.app_problems()
    health = [("GitHub App", not app_problems,
               "; ".join(app_problems) or f"app {settings.github_app_id}")]
    for tool in SCANNERS:
        path = tools.find_tool(tool)
        health.append((tool, path is not None,
                       path or "not installed: no PR can be merged until it is"))
    provider = analyzer_settings.llm_provider.strip().lower()
    model = analyzer_settings.llm_model or DEFAULT_MODELS.get(provider, "?")
    if settings.review_app_skip_llm:
        health.append(("AI review", False, "switched off (REVIEW_APP_SKIP_LLM)"))
    elif provider == "nvidia":
        has_key = bool(analyzer_settings.nvidia_api_key)
        health.append(("AI review", has_key, f"NVIDIA NIM, {model}"
                       + ("" if has_key else ": NVIDIA_API_KEY is not set")))
    else:
        health.append(("AI review", True, f"{provider}, {model} (not NVIDIA)"))
    auto = policies.auto_repos()
    health.append(("Policy", True, f"{policies.source}; auto-merge "
                   + (f"on for {', '.join(auto)}" if auto else "off everywhere")))
    return health


@app.command()
def webhook(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for GitHub's deliveries")] = 8000,
    worker: Annotated[bool, typer.Option("--worker/--no-worker",
                                         help="Run the worker in this process")] = True,
) -> None:
    """Receive GitHub's webhooks (POST /webhook) and work through the jobs they queue."""
    setup_logging("INFO")
    settings, policies, store = load()
    problems = settings.app_problems()
    if problems:
        fail("The webhook server needs the GitHub App's credentials:", *problems,
             "See README.md, 'Create the GitHub App'.")
    try:
        tokens = InstallationTokens(settings.github_app_id or 0, settings.private_key())
    except SettingsError as e:
        fail(str(e))
    queue = JobQueue(store)
    handler = WebhookHandler(store, queue, app_id=settings.github_app_id)
    runners: list[Callable[[threading.Event], None]] = []
    if worker:
        queue.recover_expired()
        for _ in range(settings.review_app_workers):
            loop = Worker(store, queue, policies, PrAnalyzer(), AppAccess(tokens),
                          skip_llm=settings.review_app_skip_llm)
            runners.append(partial(loop.run_forever,
                                   poll_seconds=settings.review_app_poll_seconds))
    uvicorn.run(create_webhook_app(settings, handler, runners), host=host, port=port,
                log_level="info")


@app.command()
def dashboard(
    host: Annotated[str, typer.Option(help="Loopback interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for the dashboard")] = 8001,
) -> None:
    """Serve the read-only dashboard on localhost."""
    if not is_loopback(host):
        fail("The dashboard has no login, so it only listens on a loopback address "
             "(127.0.0.1, ::1 or localhost).")
    settings, policies, _ = load()
    health = environment_health(settings, policies)
    reader = Store(settings.review_app_db, readonly=True)
    typer.echo(f"Dashboard: http://{'localhost' if host == '127.0.0.1' else host}:{port}/")
    uvicorn.run(create_dashboard_app(reader, policies, health), host=host, port=port,
                log_level="warning")


@app.command("worker")
def run_worker() -> None:
    """Work through queued jobs without serving webhooks (when `webhook --no-worker` runs)."""
    setup_logging("INFO")
    settings, policies, store = load()
    problems = settings.app_problems()
    if problems:
        fail("The worker acts as the GitHub App and needs its credentials:", *problems)
    try:
        tokens = InstallationTokens(settings.github_app_id or 0, settings.private_key())
    except SettingsError as e:
        fail(str(e))
    queue = JobQueue(store)
    queue.recover_expired()
    loop = Worker(store, queue, policies, PrAnalyzer(), AppAccess(tokens),
                  skip_llm=settings.review_app_skip_llm)
    stop = threading.Event()
    try:
        loop.run_forever(stop, settings.review_app_poll_seconds)
    except KeyboardInterrupt:
        stop.set()


@app.command()
def analyze(
    pr_url: Annotated[str, typer.Argument(help="https://github.com/<owner>/<repo>/pull/<number>")],
    post_review: Annotated[bool, typer.Option(
        "--post-review/--no-post-review",
        help="Post pr-analyzer's review on the PR (needs a GITHUB_TOKEN that can write PRs)",
    )] = False,
) -> None:
    """Analyse one PR now with GITHUB_TOKEN and print the decision. Never merges: the
    repository's policy is applied in recommend-only mode, and no check run is posted."""
    setup_logging(analyzer_settings.log_level)
    match = PR_URL.match(pr_url.strip())
    if match is None or not REPO_NAME.fullmatch(match["repo"]):
        fail("Expected https://github.com/<owner>/<repo>/pull/<number>")
    repo, number = match["repo"], int(match["number"])
    settings, policies, store = load()
    policy = policies.for_repo(repo).model_copy(
        update={"mode": "recommend", "post_review": post_review})
    one_off = PolicyBook(defaults=policy, source=policies.source)
    access = TokenAccess(analyzer_settings.github_token)
    loop = Worker(store, JobQueue(store), one_off, PrAnalyzer(), access,
                  skip_llm=settings.review_app_skip_llm, actor="cli", include_closed=True)
    # not queued, so a running webhook server's worker can't pick it up; out of tries, so a
    # passing error ends in a decision instead of a retry
    job = Job(id=0, kind="analyze", repo=repo, pr_number=number, head_sha="",
              installation_id=None, attempts=MAX_ATTEMPTS)
    typer.echo(f"Analysing {repo}#{number} (this runs pr-analyzer, Semgrep and the AI review)…",
               err=True)
    loop.process(job)
    row = store.latest_decision(repo, number)
    if row is None:
        fail("No decision was recorded: see the audit log (review-app dashboard).")
    typer.echo(f"\nDecision: {row['verdict']}  (confidence {row['confidence']:.2f}, "
               f"mode {row['mode']}, outcome {row['outcome']})")
    for reason in json.loads(row["reasons"]):
        typer.echo(f"  - {reason}")


@app.command("check-config")
def check_config() -> None:
    """Check review-app.toml and the environment, and show each repository's policy."""
    settings, policies, _ = load()
    for name, ok, note in environment_health(settings, policies):
        typer.echo(f"{'ok ' if ok else '!! '} {name}: {note}")
    typer.echo("")
    typer.echo(f"defaults: {policies.defaults.model_dump(mode='json')}")
    for name, policy in sorted(policies.repos.items()):
        typer.echo(f"{name}: {policy.model_dump(mode='json')}")


@app.command("audit-verify")
def audit_verify() -> None:
    """Check the audit log's hash chain; exits 1 if any entry was changed, removed or reordered."""
    _, _, store = load()
    intact, bad_row, checked = store.verify_audit_chain()
    if not intact:
        fail(f"The audit log's hash chain breaks at entry {bad_row} (after {checked} good ones).")
    typer.echo(f"The audit log is intact: {checked} entries check out.")
