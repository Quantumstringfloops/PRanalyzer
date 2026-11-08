"""The two web apps. They are separate, so only the webhook receiver need ever face the internet.

- The webhook app takes GitHub's deliveries on POST /webhook, and runs the worker threads.
- The dashboard is read-only (GET only, a read-only database connection). It holds no secrets,
  answers only to localhost Host headers (against DNS rebinding), and sends headers that stop
  framing, sniffing and any script."""
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import jinja2
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from review_app.db import Store
from review_app.jobs import JobQueue
from review_app.policy import PolicyBook
from review_app.settings import AppSettings
from review_app.webhooks import PayloadError, WebhookHandler, verify_signature

log = structlog.get_logger(__name__)

HERE = Path(__file__).parent
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
MAX_DELIVERY_ID = 100
PAGE_SIZE = 100
# One fixed statement for every filter combination: an empty filter switches its own condition
# off, so no SQL is ever put together from what a request sends.
AUDIT_PAGE = (
    "SELECT * FROM audit_log"
    " WHERE (:repo = '' OR repo = :repo COLLATE NOCASE)"
    " AND (:action = '' OR action LIKE :action_prefix ESCAPE '\\')"
    " AND (:before IS NULL OR id < :before)"
    " ORDER BY id DESC LIMIT :limit"
)
VERDICT_LABELS = {"merge": "Merge", "needs_review": "Needs review", "block": "Block"}


# ── webhook receiver ─────────────────────────────────────────────────────────────────────
def create_webhook_app(settings: AppSettings, handler: WebhookHandler,
                       workers: Sequence[Callable[[threading.Event], None]] = ()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        threads = [threading.Thread(target=run, args=(stop,), name=f"review-worker-{n}",
                                    daemon=True) for n, run in enumerate(workers)]
        for thread in threads:
            thread.start()
        yield
        stop.set()
        for thread in threads:
            # a job in flight keeps its lease; the next start puts it back on the queue
            thread.join(timeout=10)

    app = FastAPI(title="review-app webhooks", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        secret = settings.webhook_secret
        if not secret:
            return error(503, "GITHUB_WEBHOOK_SECRET is not set, so no delivery can be trusted")
        if not request.headers.get("content-type", "").startswith("application/json"):
            return error(415, "set the webhook's content type to application/json")
        limit = settings.review_app_max_webhook_bytes
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            return error(413, "payload too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return error(413, "payload too large")
        if not verify_signature(secret, bytes(body), request.headers.get("x-hub-signature-256")):
            log.warning("webhook_signature_rejected")
            return error(401, "bad or missing X-Hub-Signature-256")

        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        if not event or not delivery or len(delivery) > MAX_DELIVERY_ID:
            return error(400, "missing X-GitHub-Event or X-GitHub-Delivery")
        try:
            payload = json.loads(body)
        except ValueError:
            return error(400, "the body isn't JSON")
        if not isinstance(payload, dict):
            return error(400, "the body isn't a JSON object")
        try:
            result = await run_in_threadpool(handler.handle, event, delivery, payload)
        except PayloadError as e:
            return error(400, str(e))
        log.info("webhook", gh_event=event, delivery=delivery, status=result.status,
                 result=result.message)
        return JSONResponse({"message": result.message, "job": result.job_id},
                            status_code=result.status)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# ── dashboard ────────────────────────────────────────────────────────────────────────────
def host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):   # [::1]:8001
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0]


def like_prefix(text: str) -> str:
    """A LIKE pattern for values starting with `text`, its % and _ taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def create_dashboard_app(store: Store, policies: PolicyBook,
                         health: list[tuple[str, bool, str]] | None = None) -> FastAPI:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["verdict"] = lambda v: VERDICT_LABELS.get(v or "", v or "—")
    env.filters["when"] = lambda s: (s or "")[:19].replace("T", " ") + (" UTC" if s else "")
    templates = Jinja2Templates(env=env)
    queue = JobQueue(store)
    app = FastAPI(title="review-app dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        if host_name(request.headers.get("host", "")) not in LOOPBACK_NAMES:
            return PlainTextResponse("The dashboard is only served on localhost.", 403)
        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse("The dashboard is read-only.", 405)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with store.read() as conn:
            repos = conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM pull_requests p WHERE p.repo = r.full_name"
                " AND p.state = 'open') AS open_prs FROM repositories r ORDER BY r.full_name"
            ).fetchall()
            decisions = recent_decisions(conn, limit=25)
        return page(request, "index.html", repos=repos, decisions=decisions, policies=policies,
                    health=health or [], jobs=queue.counts())

    @app.get("/repos/{owner}/{name}", response_class=HTMLResponse)
    def repo(request: Request, owner: str, name: str) -> HTMLResponse:
        full_name = f"{owner}/{name}"
        with store.read() as conn:
            known = conn.execute("SELECT * FROM repositories WHERE full_name = ?",
                                 (full_name,)).fetchone()
            prs = conn.execute(
                "SELECT p.*, d.verdict, d.confidence, d.outcome, d.created_at AS decided_at,"
                " a.score, a.grade FROM pull_requests p"
                " LEFT JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions"
                "   WHERE pr_id = p.id)"
                " LEFT JOIN analyses a ON a.id = d.analysis_id"
                " WHERE p.repo = ? ORDER BY p.updated_at DESC LIMIT 200", (full_name,),
            ).fetchall()
        if known is None and not prs:
            raise HTTPException(404, "unknown repository")
        return page(request, "repo.html", full_name=known["full_name"] if known else full_name,
                    repo=known, prs=prs, policy=policies.for_repo(full_name))

    @app.get("/prs/{pr_id}", response_class=HTMLResponse)
    def pull(request: Request, pr_id: int) -> HTMLResponse:
        with store.read() as conn:
            pr = conn.execute("SELECT * FROM pull_requests WHERE id = ?", (pr_id,)).fetchone()
            if pr is None:
                raise HTTPException(404, "unknown pull request")
            decisions = [
                {**dict(d), "reasons": loads(d["reasons"], []), "facts": loads(d["facts"], {})}
                for d in conn.execute(
                    "SELECT d.*, a.score, a.grade FROM decisions d LEFT JOIN analyses a"
                    " ON a.id = d.analysis_id WHERE d.pr_id = ? ORDER BY d.id DESC LIMIT 50",
                    (pr_id,))
            ]
            latest = conn.execute("SELECT * FROM analyses WHERE pr_id = ? ORDER BY id DESC"
                                  " LIMIT 1", (pr_id,)).fetchone()
            events = conn.execute(
                "SELECT * FROM audit_log WHERE repo = ? AND pr_number = ? ORDER BY id DESC"
                " LIMIT 200", (pr["repo"], pr["number"]),
            ).fetchall()
        analysis = None
        if latest is not None:
            analysis = {**dict(latest), "findings": loads(latest["findings"], []),
                        "warnings": loads(latest["warnings"], []),
                        "dimensions": loads(latest["dimensions"], {})}
        return page(request, "pr.html", pr=pr, decisions=decisions, analysis=analysis,
                    events=[{**dict(e), "details": loads(e["details"], {})} for e in events],
                    github_url=f"https://github.com/{pr['repo']}/pull/{pr['number']}")

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, repo: str = "", action: str = "",
              before: int | None = None) -> HTMLResponse:
        with store.read() as conn:
            rows = conn.execute(AUDIT_PAGE, {
                "repo": repo, "action": action, "action_prefix": like_prefix(action),
                "before": before, "limit": PAGE_SIZE,
            }).fetchall()
        intact, bad_row, checked = store.verify_audit_chain()
        return page(request, "audit.html", entries=[{**dict(r), "details": loads(r["details"], {})}
                                                     for r in rows],
                    repo=repo, action=action, intact=intact, bad_row=bad_row, checked=checked,
                    next_before=rows[-1]["id"] if len(rows) == PAGE_SIZE else None)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request) -> HTMLResponse:
        with store.read() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                                (PAGE_SIZE,)).fetchall()
        return page(request, "jobs.html", jobs=rows, counts=queue.counts())

    @app.get("/static/style.css")
    def style() -> FileResponse:
        return FileResponse(HERE / "static" / "style.css", media_type="text/css")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "jobs": queue.counts()}

    return app


def recent_decisions(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.id, d.verdict, d.confidence, d.mode, d.outcome, d.complete, d.created_at,"
        " p.id AS pr_id, p.repo, p.number, p.title, a.score, a.grade FROM decisions d"
        " JOIN pull_requests p ON p.id = d.pr_id LEFT JOIN analyses a ON a.id = d.analysis_id"
        " ORDER BY d.id DESC LIMIT ?", (limit,),
    ).fetchall()
