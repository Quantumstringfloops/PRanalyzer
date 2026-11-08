"""Dashboard login through Supabase Auth.

Supabase keeps the users and their passwords, so review-app never stores either. Signing in
returns a Supabase session: a short-lived access token (a JWT) and a refresh token. The dashboard
keeps both in HttpOnly cookies, checks the access token's signature on every request, and
refreshes it when it runs out. Only the addresses on REVIEW_APP_ADMIN_EMAILS get in, and that is
checked on every request too, so taking someone off the list locks them out at once.

Calls go to Supabase Auth's REST API with the project's anon (publishable) key. review-app never
needs the service-role key."""
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog

log = structlog.get_logger(__name__)

AUDIENCE = "authenticated"   # the audience Supabase gives a signed-in user's access token
ASYMMETRIC = ("ES256", "RS256", "EdDSA")   # Supabase's JWT signing keys, published as a JWKS
JWKS_TTL_SECONDS = 600
JWKS_REFETCH_SECONDS = 60    # an unknown key id fetches the keys again, at most this often
LEEWAY_SECONDS = 30
TIMEOUT_SECONDS = 10.0
MAX_EMAIL = 320
MAX_PASSWORD = 1024
BAD_LOGIN = "That email and password didn't work, or this account may not use the dashboard."
NOT_SIGNED_IN = "Not signed in."
BAD_REPLY = "Supabase sent a reply review-app couldn't read."


class AuthError(Exception):
    """A sign-in, refresh or token check that failed. `message` is safe to show anyone: it never
    says whether an account exists."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class User:
    id: str
    email: str


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_in: int        # seconds the access token stays valid
    user: User

    def as_json(self) -> dict[str, Any]:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "token_type": "bearer", "expires_in": self.expires_in,
                "user": {"id": self.user.id, "email": self.user.email}}


class Allowlist:
    """Who may use the dashboard: whole addresses, or "@example.com" for everyone at a domain.
    An empty list lets nobody in."""

    def __init__(self, entries: Iterable[str]):
        cleaned = {entry.strip().lower() for entry in entries if entry.strip()}
        self.domains = frozenset(e for e in cleaned if e.startswith("@"))
        self.emails = frozenset(cleaned - self.domains)

    @classmethod
    def parse(cls, text: str) -> "Allowlist":
        return cls(re.split(r"[,\s]+", text))

    def __len__(self) -> int:
        return len(self.emails) + len(self.domains)

    def allows(self, email: str) -> bool:
        email = email.strip().lower()
        if email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
            return False
        return email in self.emails or "@" + email.split("@")[1] in self.domains


class LoginThrottle:
    """At most `limit` failed sign-ins per key (a client and the address it tried) in `window`
    seconds. Supabase rate-limits too, but it sees every attempt as coming from this server."""

    def __init__(self, limit: int = 5, window: float = 900.0,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def failed(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(self.clock())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _recent(self, key: str) -> deque[float]:
        times = self._failures.setdefault(key, deque())
        while times and times[0] <= self.clock() - self.window:
            times.popleft()
        return times


class SupabaseAuth:
    """Supabase Auth's REST API (sign in, refresh, sign out), and the access-token check.

    Access tokens are checked here rather than by asking Supabase each time. Projects on
    Supabase's JWT signing keys (the default for new projects) publish them at
    /auth/v1/.well-known/jwks.json. Projects still on the legacy shared secret need it in
    SUPABASE_JWT_SECRET."""

    def __init__(self, url: str, anon_key: str, jwt_secret: str = "",
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url.rstrip("/")
        self.issuer = f"{self.url}/auth/v1"
        self.jwt_secret = jwt_secret
        self.clock = clock
        self.http = httpx.Client(
            base_url=self.issuer, timeout=TIMEOUT_SECONDS, transport=transport,
            headers={"apikey": anon_key, "Accept": "application/json",
                     "X-Client-Info": "review-app"},
        )
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_fetched: float | None = None
        self._lock = threading.Lock()

    # ── the REST API ─────────────────────────────────────────────────────────────────────
    def sign_in(self, email: str, password: str) -> Session:
        data = self._call("POST", "/token", params={"grant_type": "password"},
                          json={"email": email, "password": password}, refused=BAD_LOGIN)
        return self._session(data)

    def refresh(self, refresh_token: str) -> Session:
        data = self._call("POST", "/token", params={"grant_type": "refresh_token"},
                          json={"refresh_token": refresh_token},
                          refused="Your session has ended; sign in again.")
        return self._session(data)

    def sign_out(self, access_token: str) -> None:
        """End the session at Supabase, which revokes its refresh token. An access token already
        handed out still works until it expires (an hour, by Supabase's default)."""
        try:
            self._call("POST", "/logout", params={"scope": "local"},
                       headers={"Authorization": f"Bearer {access_token}"}, refused=NOT_SIGNED_IN)
        except AuthError as e:
            # already expired or revoked: the cookies go anyway
            log.info("supabase_sign_out_refused", status=e.status)

    def _call(self, method: str, path: str, *, refused: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            log.warning("supabase_unreachable", error=str(e))
            raise AuthError(503, "Couldn't reach Supabase; try again shortly.") from e
        if response.status_code == 429:
            raise AuthError(429, "Too many attempts; wait a minute and try again.")
        if response.status_code >= 500:
            log.warning("supabase_error", path=path, status=response.status_code)
            raise AuthError(503, "Supabase had a problem; try again shortly.")
        if response.status_code == 401 and "api key" in response.text.lower():
            log.error("supabase_key_refused")
            raise AuthError(503, "Supabase refused review-app's key: check SUPABASE_ANON_KEY.")
        if response.status_code >= 400:
            log.info("supabase_refused", path=path, status=response.status_code,
                     code=error_code(response))
            raise AuthError(401, refused)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as e:
            raise AuthError(502, BAD_REPLY) from e
        if not isinstance(data, dict):
            raise AuthError(502, BAD_REPLY)
        return data

    @staticmethod
    def _session(data: dict[str, Any]) -> Session:
        try:
            user = data["user"]
            return Session(access_token=str(data["access_token"]),
                           refresh_token=str(data["refresh_token"]),
                           expires_in=int(data.get("expires_in") or 3600),
                           user=User(id=str(user["id"]), email=str(user.get("email") or "")))
        except (KeyError, TypeError, ValueError) as e:
            raise AuthError(502, BAD_REPLY) from e

    # ── access tokens ────────────────────────────────────────────────────────────────────
    def verify(self, token: str) -> User:
        """Whose access token this is, once its signature, issuer, audience and expiry check out.
        AuthError(401) for anything else, including a token signed some other way."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError(401, NOT_SIGNED_IN) from e
        alg, kid = header.get("alg"), str(header.get("kid") or "")
        key: Any
        if alg == "HS256" and self.jwt_secret:
            key = self.jwt_secret
        elif alg in ASYMMETRIC:
            signing_key = self._signing_key(kid)
            if signing_key is None or signing_key.algorithm_name != alg:
                raise AuthError(401, NOT_SIGNED_IN)
            key = signing_key.key
        else:
            raise AuthError(401, NOT_SIGNED_IN)
        try:
            claims = jwt.decode(token, key, algorithms=[alg], audience=AUDIENCE,
                                issuer=self.issuer, leeway=LEEWAY_SECONDS,
                                options={"require": ["exp", "sub", "aud", "iss"]})
        except jwt.PyJWTError as e:
            raise AuthError(401, NOT_SIGNED_IN) from e
        return User(id=str(claims["sub"]), email=str(claims.get("email") or ""))

    def _signing_key(self, kid: str) -> jwt.PyJWK | None:
        with self._lock:
            now = self.clock()
            age = None if self._keys_fetched is None else now - self._keys_fetched
            # fetch when the keys are old, or when a key id is new (a rotation), but not more
            # than once a minute however many unknown ids arrive
            if age is None or age > JWKS_TTL_SECONDS or (
                    kid not in self._keys and age > JWKS_REFETCH_SECONDS):
                self._keys = self._fetch_keys()
                self._keys_fetched = now
            return self._keys.get(kid)

    def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        try:
            response = self.http.get("/.well-known/jwks.json")
            response.raise_for_status()
            entries = response.json().get("keys", [])
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.warning("supabase_jwks_unavailable", error=str(e))
            return self._keys    # keep the keys already known
        keys: dict[str, jwt.PyJWK] = {}
        for entry in entries if isinstance(entries, list) else []:
            try:
                key = jwt.PyJWK(entry)
            except (jwt.PyJWTError, TypeError):
                continue
            if key.algorithm_name in ASYMMETRIC and entry.get("kid"):
                keys[str(entry["kid"])] = key
        return keys


def error_code(response: httpx.Response) -> str:
    """Supabase's own code for a refusal ("invalid_credentials"), for the log."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("error_code") or data.get("error") or data.get("code") or "")


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a path on this site only, never another site."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    if any(ch in target for ch in "\r\n\\") or target.startswith(("/auth/", "/login")):
        return "/"
    return target


@dataclass
class Login:
    """Everything the dashboard needs to require a login."""

    auth: SupabaseAuth
    allowlist: Allowlist
    hosts: frozenset[str] = frozenset()    # Host names served besides localhost
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
