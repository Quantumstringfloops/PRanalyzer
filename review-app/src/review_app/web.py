"""The two web apps. They are separate, so only the webhook receiver need ever face the internet.

- The webhook app takes GitHub's deliveries on POST /webhook, and runs the worker threads.
- The dashboard is read-only (GET only, a read-only database connection). It answers only to
  localhost Host headers (against DNS rebinding), and sends headers that stop framing, sniffing
  and any script. With login on (Supabase Auth, see auth.py) it also answers to the host names
  configured for it, every page needs a signed-in admin, and the only POSTs are the sign-in,
  sign-out and refresh endpoints."""
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import jinja2
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from review_app import view
from review_app.auth import (
    BAD_LOGIN,
    MAX_EMAIL,
    MAX_PASSWORD,
    AuthError,
    Login,
    Session,
    User,
    safe_next,
)
from review_app.db import Store
from review_app.jobs import JobQueue
from review_app.policy import PolicyBook
from review_app.settings import AppSettings
from review_app.webhooks import PayloadError, WebhookHandler, verify_signature

log = structlog.get_logger(__name__)

HERE = Path(__file__).parent
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
SECURITY_HEADERS = {
    # no script at all, and no inline style; the one outside source is the Oxanium font (the
    # shadcn preset's) from Google Fonts: its stylesheet and the font files
    "Content-Security-Policy": "default-src 'none'; style-src 'self' https://fonts.googleapis.com; "
                               "font-src https://fonts.gstatic.com; img-src 'self'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # nothing to other sites; same-origin rather than no-referrer, which would make browsers send
    # "Origin: null" with the sign-in form's POST, and the cross-site check refuse it
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
MAX_DELIVERY_ID = 100
PAGE_SIZE = 100
# the only files /static/ serves, with their types
STATIC_FILES = {"style.css": "text/css", "favicon.svg": "image/svg+xml"}
NOTHING_HERE = "There's nothing at this address."
# dashboard login: Supabase's tokens in HttpOnly cookies, and the only POSTs the dashboard takes
ACCESS_COOKIE = "review_app_access"
REFRESH_COOKIE = "review_app_refresh"
REFRESH_MAX_AGE = 7 * 24 * 3600     # sign in again after a week
LOGIN_POSTS = frozenset({"/auth/login", "/auth/logout", "/auth/refresh"})
MAX_FORM_BYTES = 16 * 1024
# One fixed statement for every filter combination: an empty filter switches its own condition
# off, so no SQL is ever put together from what a request sends.
AUDIT_PAGE = (
    "SELECT a.*, p.id AS pr_id FROM audit_log a"
    " LEFT JOIN pull_requests p ON p.repo = a.repo AND p.number = a.pr_number"
    " WHERE (:repo = '' OR a.repo = :repo COLLATE NOCASE)"
    " AND (:action = '' OR a.action LIKE :action_prefix ESCAPE '\\')"
    " AND (:before IS NULL OR a.id < :before)"
    " ORDER BY a.id DESC LIMIT :limit"
)
# decisions per day, for the overview's activity chart
ACTIVITY = (
    "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM decisions"
    " WHERE created_at >= ? GROUP BY day"
)
ACTIVITY_DAYS = 14
# the latest decision on each pull request, counted by verdict
LATEST_VERDICTS = (
    "SELECT verdict, COUNT(*) AS n FROM decisions"
    " WHERE id IN (SELECT MAX(id) FROM decisions GROUP BY pr_id) GROUP BY verdict"
)
REPOSITORIES = (
    "SELECT r.*, (SELECT COUNT(*) FROM pull_requests p WHERE p.repo = r.full_name"
    " AND p.state = 'open') AS open_prs FROM repositories r ORDER BY r.full_name"
)
# a repository's pull requests, each with its latest decision; the columns match
# recent_decisions(), so both lists render with the same template macro
REPOSITORY_PRS = (
    "SELECT p.id AS pr_id, p.repo, p.number, p.title, p.author, p.state, p.is_fork,"
    " p.is_draft, p.updated_at, d.verdict, d.confidence, d.outcome, d.complete, d.created_at,"
    " a.score, a.grade FROM pull_requests p"
    " LEFT JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE pr_id = p.id)"
    " LEFT JOIN analyses a ON a.id = d.analysis_id"
    " WHERE p.repo = ? ORDER BY p.updated_at DESC LIMIT 200"
)
JOBS_PAGE = (
    "SELECT j.*, p.id AS pr_id FROM jobs j"
    " LEFT JOIN pull_requests p ON p.repo = j.repo AND p.number = j.pr_number"
    " ORDER BY j.id DESC LIMIT ?"
)


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
                         health: list[tuple[str, bool, str]] | None = None,
                         login: Login | None = None) -> FastAPI:
    """The dashboard. Without `login` it answers on localhost only, to anyone who can reach it
    there; with it, every page needs a signed-in admin."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters.update(
        verdict=lambda v: view.VERDICT_LABELS.get(v or "", v or "Not decided"),
        outcome=lambda v: view.OUTCOME_LABELS.get(v or "", (v or "").replace("_", " ")),
        when=view.when, clock=view.clock, ago=view.ago, day=view.day_label, pct=view.percent,
        action_label=view.action_label,
    )
    env.globals.update(
        policies=policies, auto_repos=policies.auto_repos(),
        score_tone=view.score_tone, grade_tone=view.grade_tone,
        confidence_tone=view.confidence_tone, action_tone=view.action_tone,
        verdict_tones=view.VERDICT_TONES, outcome_tones=view.OUTCOME_TONES,
        outcome_notes=view.OUTCOME_NOTES, severity_tones=view.SEVERITY_TONES,
        job_tones=view.JOB_TONES, login_on=login is not None, ring=view.ring,
    )
    templates = Jinja2Templates(env=env)
    queue = JobQueue(store)
    app = FastAPI(title="review-app dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    served_hosts = LOOPBACK_NAMES | (login.hosts if login else frozenset())

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        request.state.user = None
        host = host_name(request.headers.get("host", ""))
        if host not in served_hosts:
            return secured(PlainTextResponse("The dashboard isn't served on this host name.", 403))
        path = request.url.path
        if request.method not in ("GET", "HEAD"):
            if login is None or request.method != "POST" or path not in LOGIN_POSTS:
                return secured(PlainTextResponse("The dashboard is read-only.", 405))
            if not same_origin(request):
                return secured(PlainTextResponse("Cross-site requests are refused.", 403))
        renewed, stale = None, False
        if login is not None and checks_login(path):
            user, renewed, stale = await run_in_threadpool(identify, login, request)
            request.state.user = user
            if user is None and path != "/login":
                response = login_required(request)
                if stale:
                    clear_session(response, secure=host not in LOOPBACK_NAMES)
                return secured(response)
        response = await call_next(request)
        if renewed is not None:
            set_session(response, renewed, secure=host not in LOOPBACK_NAMES)
        elif stale:
            clear_session(response, secure=host not in LOOPBACK_NAMES)
        return secured(response)

    def page(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next_page: str = Query("/", alias="next")) -> Response:
        target = safe_next(next_page)
        if request.state.user is not None:
            return RedirectResponse(target, status_code=303)
        return page(request, "login.html", next=target, email="", error="")

    if login is not None:
        install_login_routes(app, login, page)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        # Starlette's own 404, for an address no route matches, says only "Not Found"
        message = NOTHING_HERE if exc.detail == "Not Found" else exc.detail
        return page(request, "error.html", exc.status_code, status=exc.status_code,
                    message=message)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError) -> HTMLResponse:
        # /prs/abc and the like: nothing lives there
        return page(request, "error.html", 404, status=404, message=NOTHING_HERE)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with store.read() as conn:
            repos = conn.execute(REPOSITORIES).fetchall()
            decisions = recent_decisions(conn, limit=25)
            verdicts = {row["verdict"]: row["n"] for row in conn.execute(LATEST_VERDICTS)}
            merged = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE outcome = 'merged'").fetchone()[0]
            first_day = datetime.now(UTC).date() - timedelta(days=ACTIVITY_DAYS - 1)
            per_day = {row["day"]: row["n"]
                       for row in conn.execute(ACTIVITY, (first_day.isoformat(),))}
        checks = health or []
        activity = view.daily_counts(per_day, days=ACTIVITY_DAYS)
        return page(request, "index.html", repos=repos, decisions=decisions, health=checks,
                    ready=sum(1 for _, ok, _ in checks if ok), jobs=queue.counts(),
                    split=view.shares({v: verdicts.get(v, 0) for v in view.VERDICT_LABELS}),
                    graded=sum(verdicts.values()), merged=merged, activity=activity,
                    chart=view.area_chart([n for _, n in activity]))

    @app.get("/repos/{owner}/{name}", response_class=HTMLResponse)
    def repo(request: Request, owner: str, name: str) -> HTMLResponse:
        full_name = f"{owner}/{name}"
        with store.read() as conn:
            known = conn.execute("SELECT * FROM repositories WHERE full_name = ?",
                                 (full_name,)).fetchone()
            prs = conn.execute(REPOSITORY_PRS, (full_name,)).fetchall()
        if known is None and not prs:
            raise HTTPException(404, "There's no repository by that name here.")
        return page(request, "repo.html", full_name=known["full_name"] if known else full_name,
                    repo=known, prs=prs, policy=policies.for_repo(full_name))

    @app.get("/prs/{pr_id}", response_class=HTMLResponse)
    def pull(request: Request, pr_id: int) -> HTMLResponse:
        with store.read() as conn:
            pr = conn.execute("SELECT * FROM pull_requests WHERE id = ?", (pr_id,)).fetchone()
            if pr is None:
                raise HTTPException(404, "There's no pull request with that number here.")
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
                    groups=view.group_findings(analysis["findings"]) if analysis else [],
                    policy=policies.for_repo(pr["repo"]),
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
                    repo=repo, action=action, before=before, intact=intact, bad_row=bad_row,
                    checked=checked,
                    next_before=rows[-1]["id"] if len(rows) == PAGE_SIZE else None)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request) -> HTMLResponse:
        with store.read() as conn:
            rows = conn.execute(JOBS_PAGE, (PAGE_SIZE,)).fetchall()
        return page(request, "jobs.html", jobs=rows, counts=queue.counts())

    @app.get("/static/{name}")
    def static(name: str) -> FileResponse:
        media_type = STATIC_FILES.get(name)
        if media_type is None:
            raise HTTPException(404, "There's no such file.")
        return FileResponse(HERE / "static" / name, media_type=media_type)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        # open to anyone for a load balancer's checks, so with login on it says nothing more
        return {"ok": True} if login is not None else {"ok": True, "jobs": queue.counts()}

    return app


# ── dashboard login ──────────────────────────────────────────────────────────────────────
PageRenderer = Callable[..., HTMLResponse]


def install_login_routes(app: FastAPI, login: Login, page: PageRenderer) -> None:
    """POST /auth/login, /auth/logout and /auth/refresh, and GET /auth/me. Each takes the
    dashboard's own form posts (and redirects), or JSON (and answers in JSON)."""

    @app.post("/auth/login")
    async def sign_in(request: Request) -> Response:
        answer_json = is_json(request)
        fields: dict[str, str] = {}
        try:
            fields = await read_form(request)
            session = await check_credentials(login, request, fields)
        except AuthError as e:
            if answer_json:
                return JSONResponse({"error": e.message}, status_code=e.status)
            return page(request, "login.html", e.status, next=safe_next(fields.get("next")),
                        email=fields.get("email", "")[:MAX_EMAIL], error=e.message)
        log.info("signed_in", user=session.user.id, email=session.user.email)
        response: Response = (JSONResponse(session.as_json()) if answer_json
                              else RedirectResponse(safe_next(fields.get("next")), status_code=303))
        set_session(response, session, secure=not on_loopback(request))
        return response

    @app.post("/auth/logout")
    async def sign_out(request: Request) -> Response:
        token = bearer(request) or request.cookies.get(ACCESS_COOKIE)
        refresh_token = request.cookies.get(REFRESH_COOKIE)
        if not token and refresh_token:
            # the access token ran out: renew it just to end the session at Supabase
            with suppress(AuthError):
                token = (await run_in_threadpool(login.auth.refresh, refresh_token)).access_token
        if token:
            await run_in_threadpool(login.auth.sign_out, token)
        response: Response = (Response(status_code=204) if is_json(request)
                              else RedirectResponse("/login", status_code=303))
        clear_session(response, secure=not on_loopback(request))
        return response

    @app.post("/auth/refresh")
    async def refresh(request: Request) -> Response:
        try:
            fields = await read_form(request)
            token = fields.get("refresh_token") or request.cookies.get(REFRESH_COOKIE) or ""
            if not token:
                raise AuthError(401, "Not signed in.")
            session = await run_in_threadpool(login.auth.refresh, token)
            if not login.allowlist.allows(session.user.email):
                await run_in_threadpool(login.auth.sign_out, session.access_token)
                raise AuthError(401, "Not signed in.")
        except AuthError as e:
            response: Response = JSONResponse({"error": e.message}, status_code=e.status)
            clear_session(response, secure=not on_loopback(request))
            return response
        response = JSONResponse(session.as_json())
        set_session(response, session, secure=not on_loopback(request))
        return response

    @app.get("/auth/me")
    def me(request: Request) -> JSONResponse:
        user: User = request.state.user     # the guard let the request through: someone's in
        return JSONResponse({"id": user.id, "email": user.email})


async def check_credentials(login: Login, request: Request, fields: dict[str, str]) -> Session:
    """Sign in with Supabase, for an address on the allowlist that isn't being throttled."""
    email = fields.get("email", "").strip()
    password = fields.get("password", "")
    client = request.client.host if request.client else "unknown"
    key = f"{client}|{email.lower()}"
    if login.throttle.blocked(key):
        raise AuthError(429, "Too many failed sign-ins. Wait 15 minutes and try again.")
    if not (0 < len(email) <= MAX_EMAIL and 0 < len(password) <= MAX_PASSWORD):
        login.throttle.failed(key)
        raise AuthError(401, BAD_LOGIN)
    if not login.allowlist.allows(email):
        # refused before Supabase hears of it, with the same answer as a wrong password
        login.throttle.failed(key)
        log.info("sign_in_refused", email=email, reason="not on REVIEW_APP_ADMIN_EMAILS")
        raise AuthError(401, BAD_LOGIN)
    try:
        session = await run_in_threadpool(login.auth.sign_in, email, password)
    except AuthError as e:
        if e.status == 401:
            login.throttle.failed(key)
        raise
    if not login.allowlist.allows(session.user.email):
        await run_in_threadpool(login.auth.sign_out, session.access_token)
        raise AuthError(401, BAD_LOGIN)
    login.throttle.succeeded(key)
    return session


def identify(login: Login, request: Request) -> tuple[User | None, Session | None, bool]:
    """Who sent the request: the signed-in admin, a session renewed for them when the access
    token had run out, and whether the cookies it carried are dead and should go."""
    presented = bearer(request)
    token = presented or request.cookies.get(ACCESS_COOKIE)
    if token:
        try:
            user = login.auth.verify(token)
        except AuthError:
            user = None
        if user is not None:
            allowed = login.allowlist.allows(user.email)
            return (user if allowed else None), None, not allowed
    refresh_token = None if presented else request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        return None, None, bool(token)
    try:
        session = login.auth.refresh(refresh_token)
    except AuthError:
        return None, None, True
    if not login.allowlist.allows(session.user.email):
        login.auth.sign_out(session.access_token)
        return None, None, True
    return session.user, session, False


def checks_login(path: str) -> bool:
    """Every page needs a signed-in admin but the login page itself, the stylesheet and icon,
    the health check and the sign-in endpoints."""
    return not (path.startswith("/static/") or path == "/healthz" or path in LOGIN_POSTS)


def login_required(request: Request) -> Response:
    if request.url.path.startswith("/auth/") or is_json(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def same_origin(request: Request) -> bool:
    """A POST from the dashboard's own pages, or from a client that isn't a browser. Browsers
    send Origin with every cross-site POST, and the session cookies are SameSite=Lax too."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    return urlsplit(source).netloc.lower() == request.headers.get("host", "").lower()


def on_loopback(request: Request) -> bool:
    return host_name(request.headers.get("host", "")) in LOOPBACK_NAMES


def bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


def is_json(request: Request) -> bool:
    """A JSON client rather than the dashboard's own forms."""
    if request.headers.get("content-type", "").startswith("application/json"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


async def read_form(request: Request) -> dict[str, str]:
    """The fields of a small urlencoded form or JSON object; text values only."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FORM_BYTES:
        raise AuthError(413, "That request is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise AuthError(413, "That request is too large.")
    if not body:
        return {}
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if kind == "application/json":
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return {str(k): v for k, v in data.items() if isinstance(v, str)}
        if kind == "application/x-www-form-urlencoded":
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
            return {name: values[0] for name, values in fields.items()}
    except (ValueError, UnicodeDecodeError) as e:
        raise AuthError(400, "The request's body couldn't be read.") from e
    raise AuthError(415, "Send a form, or JSON.")


def set_session(response: Response, session: Session, secure: bool) -> None:
    # HttpOnly, so no script can read them; Secure everywhere but plain-http localhost
    response.set_cookie(ACCESS_COOKIE, session.access_token, max_age=session.expires_in,
                        path="/", secure=secure, httponly=True, samesite="lax")
    response.set_cookie(REFRESH_COOKIE, session.refresh_token, max_age=REFRESH_MAX_AGE,
                        path="/", secure=secure, httponly=True, samesite="lax")


def clear_session(response: Response, secure: bool) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def secured(response: Response) -> Response:
    response.headers.update(SECURITY_HEADERS)
    return response


def recent_decisions(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.id, d.verdict, d.confidence, d.mode, d.outcome, d.complete, d.created_at,"
        " p.id AS pr_id, p.repo, p.number, p.title, p.author, p.state, p.is_fork, p.is_draft,"
        " a.score, a.grade FROM decisions d"
        " JOIN pull_requests p ON p.id = d.pr_id LEFT JOIN analyses a ON a.id = d.analysis_id"
        " ORDER BY d.id DESC LIMIT ?", (limit,),
    ).fetchall()
