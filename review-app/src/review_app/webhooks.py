"""GitHub webhooks: checking the signature, and turning events into jobs.

The payload is trusted only after its HMAC-SHA256 signature checks out against the webhook
secret. Even then, what it says about a PR is just a trigger: the worker reads the PR again
before acting."""
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from review_app.db import Store
from review_app.jobs import JobQueue
from review_app.models import PullInfo
from review_app.policy import REPO_NAME

ACTOR = "webhook"
SIGNATURE_PREFIX = "sha256="
ANALYZE_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
PASSING_CHECKS = frozenset({"success", "neutral", "skipped"})


class PayloadError(ValueError):
    """A signed payload without the fields its event should have."""


@dataclass(frozen=True)
class WebhookResult:
    status: int
    message: str
    job_id: int | None = None


def sign(secret: bytes, body: bytes) -> str:
    return SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Whether X-Hub-Signature-256 is the body's HMAC under `secret`, compared in constant time."""
    if not secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    # bytes on both sides: compare_digest raises TypeError on non-ASCII str
    return hmac.compare_digest(sign(secret, body).encode(), header.encode("utf-8", "replace"))


# ── payload fields ───────────────────────────────────────────────────────────────────────
def _dict(data: Any, key: str) -> dict[str, Any]:
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, dict):
        raise PayloadError(f"payload has no {key!r} object")
    return value


def _str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadError(f"payload has no {key!r} text")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"payload has no {key!r} number")
    return value


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def repo_name(data: dict[str, Any]) -> str:
    name = _str(data, "full_name")
    if not REPO_NAME.fullmatch(name):
        raise PayloadError(f"not a repository name: {name[:100]!r}")
    return name


def installation_id(payload: dict[str, Any]) -> int | None:
    installation = payload.get("installation")
    value = installation.get("id") if isinstance(installation, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def pull_from_payload(payload: dict[str, Any]) -> PullInfo:
    pr = _dict(payload, "pull_request")
    head = _dict(pr, "head")
    base = _dict(pr, "base")
    base_repo = _dict(base, "repo")
    head_repo = head.get("repo")   # null once a fork is deleted
    head_repo_id = head_repo.get("id") if isinstance(head_repo, dict) else None
    user = pr.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    title = pr.get("title")
    return PullInfo(
        repo=repo_name(_dict(payload, "repository")),
        number=_int(pr, "number"),
        title=title if isinstance(title, str) else "",
        author=str(login or ""),
        head_sha=_str(head, "sha"),
        base_branch=_str(base, "ref"),
        default_branch=str(base_repo.get("default_branch") or ""),
        head_repo_id=head_repo_id if isinstance(head_repo_id, int) else None,
        base_repo_id=_int(base_repo, "id"),
        is_draft=bool(pr.get("draft")),
        state="merged" if pr.get("merged") else _str(pr, "state"),
        changed_files=_count(pr, "changed_files"),
        additions=_count(pr, "additions"),
        deletions=_count(pr, "deletions"),
    )


class WebhookHandler:
    def __init__(self, store: Store, queue: JobQueue, app_id: int | None = None):
        self.store = store
        self.queue = queue
        self.app_id = app_id

    def handle(self, event: str, delivery: str, payload: dict[str, Any]) -> WebhookResult:
        if self._seen(delivery):
            return WebhookResult(200, "already handled this delivery")
        handlers = {
            "ping": lambda _: WebhookResult(200, "pong"),
            "pull_request": self._pull_request,
            "installation": self._installation,
            "installation_repositories": self._installation_repositories,
            "check_suite": self._check_suite,
            "status": self._status,
        }
        handler = handlers.get(event)
        result = handler(payload) if handler else WebhookResult(202, f"ignored {event} event")
        # recorded once handled, so a delivery that failed can be redelivered from GitHub
        self.store.first_delivery(delivery, event)
        return result

    def _seen(self, delivery: str) -> bool:
        with self.store.read() as conn:
            return conn.execute("SELECT 1 FROM deliveries WHERE id = ?", (delivery,)).fetchone() is not None

    def _pull_request(self, payload: dict[str, Any]) -> WebhookResult:
        action = str(payload.get("action") or "")
        pull = pull_from_payload(payload)
        installation = installation_id(payload)
        with self.store.transaction() as conn:
            self.store.upsert_repo(conn, pull.repo, installation, pull.default_branch or None)
            self.store.upsert_pr(conn, pull)

        if action == "closed":
            cancelled = self.queue.cancel_for_pr(pull.repo, pull.number)
            self.store.audit(ACTOR, "pr.closed", pull.repo, pull.number, pull.head_sha,
                             merged=pull.state == "merged", cancelled_jobs=cancelled)
            return WebhookResult(202, "closed")
        base_changed = action == "edited" and "base" in (payload.get("changes") or {})
        if action not in ANALYZE_ACTIONS and not base_changed:
            return WebhookResult(202, f"nothing to do for pull_request.{action}")
        if pull.state != "open":
            return WebhookResult(202, f"the PR is {pull.state}")

        job_id = self.queue.enqueue("analyze", pull.repo, pull.number, pull.head_sha, installation)
        if job_id is None:
            return WebhookResult(202, "an analysis of this commit is already queued")
        self.store.audit(ACTOR, "job.queued", pull.repo, pull.number, pull.head_sha, job=job_id,
                         kind="analyze", trigger=f"pull_request.{action}")
        return WebhookResult(202, "analysis queued", job_id)

    def _installation(self, payload: dict[str, Any]) -> WebhookResult:
        action = str(payload.get("action") or "")
        installation = _dict(payload, "installation")
        number = _int(installation, "id")
        account = installation.get("account")
        login = account.get("login") if isinstance(account, dict) else None
        names = [repo_name(r) for r in payload.get("repositories") or [] if isinstance(r, dict)]
        with self.store.transaction() as conn:
            if action in ("deleted", "suspend"):
                self.store.deactivate_installation(conn, number)
            else:
                for name in names:
                    self.store.upsert_repo(conn, name, number)
            self.store.audit(ACTOR, f"installation.{action}", conn=conn, installation=number,
                             account=login, repositories=names)
        return WebhookResult(202, f"installation {action}")

    def _installation_repositories(self, payload: dict[str, Any]) -> WebhookResult:
        number = _int(_dict(payload, "installation"), "id")
        added = [repo_name(r) for r in payload.get("repositories_added") or []
                 if isinstance(r, dict)]
        removed = [repo_name(r) for r in payload.get("repositories_removed") or []
                   if isinstance(r, dict)]
        with self.store.transaction() as conn:
            for name in added:
                self.store.upsert_repo(conn, name, number, active=True)
            for name in removed:
                self.store.upsert_repo(conn, name, number, active=False)
            self.store.audit(ACTOR, "installation.repositories_changed", conn=conn,
                             installation=number, added=added, removed=removed)
        return WebhookResult(202, f"{len(added)} added, {len(removed)} removed")

    def _check_suite(self, payload: dict[str, Any]) -> WebhookResult:
        suite = _dict(payload, "check_suite")
        if payload.get("action") != "completed" or suite.get("conclusion") not in PASSING_CHECKS:
            return WebhookResult(202, "nothing to do for this check suite")
        app = suite.get("app")
        if self.app_id is not None and isinstance(app, dict) and app.get("id") == self.app_id:
            return WebhookResult(202, "our own check suite")
        return self._checks_passed(payload, _str(suite, "head_sha"), "check_suite.completed")

    def _status(self, payload: dict[str, Any]) -> WebhookResult:
        if payload.get("state") != "success":
            return WebhookResult(202, "nothing to do for this status")
        return self._checks_passed(payload, _str(payload, "sha"), "status.success")

    def _checks_passed(self, payload: dict[str, Any], sha: str, trigger: str) -> WebhookResult:
        """Merges GitHub held back for pending checks on this commit get another try."""
        repo = repo_name(_dict(payload, "repository"))
        installation = installation_id(payload) or self.store.repo_installation(repo)
        queued = []
        for row in self.store.waiting_merges(repo, sha):
            job_id = self.queue.enqueue("merge", repo, row["number"], sha, installation)
            if job_id is not None:
                queued.append(job_id)
                self.store.audit(ACTOR, "job.queued", repo, row["number"], sha, job=job_id,
                                 kind="merge", trigger=trigger)
        return WebhookResult(202, f"{len(queued)} merge(s) queued", queued[0] if queued else None)
