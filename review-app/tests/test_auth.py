"""Dashboard login through Supabase Auth: the REST calls, the access-token check, and the
dashboard's endpoints and guard, against a fake Supabase."""
import json
import re
import time

import httpx
import jwt
import pytest
from conftest import SHA, FakeAccess, FakeAnalyzer, FakeGateway, make_pull
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from review_app.auth import (
    BAD_LOGIN,
    Allowlist,
    AuthError,
    Login,
    LoginThrottle,
    SupabaseAuth,
    safe_next,
)
from review_app.db import Store
from review_app.policy import PolicyBook
from review_app.settings import AppSettings
from review_app.web import ACCESS_COOKIE, REFRESH_COOKIE, SECURITY_HEADERS, create_dashboard_app
from review_app.worker import Worker

URL = "https://proj.supabase.co"
ISSUER = f"{URL}/auth/v1"
ANON = "anon-key"
LEGACY_SECRET = "the-legacy-jwt-secret-is-at-least-32-bytes"
ADMIN = "admin@example.com"
PASSWORD = "correct horse battery staple"
OUTSIDER = "outsider@elsewhere.example"


class FakeSupabase:
    """Supabase Auth's REST API: one password per address, ES256 signing keys published as a
    JWKS (or the legacy HS256 secret), and refresh tokens that each work once."""

    def __init__(self, signing: str = "ES256"):
        self.passwords = {ADMIN: PASSWORD, OUTSIDER: "hunter22"}
        self.signing = signing
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.refresh_tokens: dict[str, str] = {}
        self.calls: list[str] = []
        self.signed_out: list[str] = []
        self.down = False
        self.rate_limited = False
        self.issued = 0

    def token(self, email: str = ADMIN, expires_in: int = 3600, key=None, **claims) -> str:
        now = int(time.time())
        body = {"sub": f"id-{email}", "email": email, "aud": "authenticated",
                "role": "authenticated", "iss": ISSUER, "iat": now, "exp": now + expires_in}
        body.update(claims)
        if self.signing == "HS256":
            return jwt.encode(body, LEGACY_SECRET, algorithm="HS256")
        return jwt.encode(body, key or self.key, algorithm="ES256", headers={"kid": "key-1"})

    def session(self, email: str) -> dict:
        self.issued += 1
        refresh = f"refresh-{self.issued}"
        self.refresh_tokens[refresh] = email
        return {"access_token": self.token(email), "token_type": "bearer", "expires_in": 3600,
                "refresh_token": refresh, "user": {"id": f"id-{email}", "email": email}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/auth/v1")
        self.calls.append(path)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("apikey") != ANON:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if self.rate_limited:
            return httpx.Response(429, json={"msg": "Request rate limit reached"})
        if path == "/.well-known/jwks.json":
            public = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            keys = [{**public, "kid": "key-1", "alg": "ES256"}] if self.signing == "ES256" else []
            return httpx.Response(200, json={"keys": keys})
        body = json.loads(request.content or b"{}")
        grant = request.url.params.get("grant_type")
        if path == "/token" and grant == "password":
            if self.passwords.get(body.get("email")) == body.get("password"):
                return httpx.Response(200, json=self.session(body["email"]))
            return httpx.Response(400, json={"code": 400, "error_code": "invalid_credentials",
                                             "msg": "Invalid login credentials"})
        if path == "/token" and grant == "refresh_token":
            email = self.refresh_tokens.pop(body.get("refresh_token"), None)
            if email is None:
                return httpx.Response(400, json={"error_code": "refresh_token_not_found"})
            return httpx.Response(200, json=self.session(email))
        if path == "/logout":
            self.signed_out.append(request.headers["authorization"].removeprefix("Bearer "))
            return httpx.Response(204)
        return httpx.Response(404)

    def auth(self, anon_key: str = ANON, clock=time.monotonic) -> SupabaseAuth:
        secret = LEGACY_SECRET if self.signing == "HS256" else ""
        return SupabaseAuth(URL, anon_key, secret, transport=httpx.MockTransport(self.handle),
                            clock=clock)


@pytest.fixture
def supabase() -> FakeSupabase:
    return FakeSupabase()


def dashboard(store, supabase, admins=ADMIN, hosts=(), base_url="http://localhost:8001",
              throttle=None) -> TestClient:
    login = Login(auth=supabase.auth(), allowlist=Allowlist.parse(admins), hosts=frozenset(hosts),
                  throttle=throttle or LoginThrottle())
    app = create_dashboard_app(Store(store.path, readonly=True), PolicyBook(), login=login)
    return TestClient(app, base_url=base_url, follow_redirects=False)


def sign_in(client, email=ADMIN, password=PASSWORD, next_page="/", **headers):
    return client.post("/auth/login", data={"email": email, "password": password,
                                            "next": next_page}, headers=headers)


def cookie_headers(response) -> dict[str, str]:
    return {header.split("=", 1)[0]: header for header in response.headers.get_list("set-cookie")}


# ── who may sign in ──────────────────────────────────────────────────────────────────────
def test_the_allowlist():
    allow = Allowlist.parse(" Admin@Example.com, @team.example\nother@x.io ")
    assert len(allow) == 3
    for email in ("admin@example.com", "ADMIN@example.com ", "anyone@team.example", "other@x.io"):
        assert allow.allows(email), email
    for email in ("someone@example.com", "a@sub.team.example", "admin@example.com.evil",
                  "@team.example", "a@b@team.example", "", "admin"):
        assert not allow.allows(email), email
    assert not Allowlist.parse("").allows(ADMIN)    # empty lets nobody in


@pytest.mark.parametrize(("target", "expected"), [
    ("/", "/"), ("/prs/1?x=1", "/prs/1?x=1"), (None, "/"), ("", "/"),
    ("//evil.example", "/"), ("https://evil.example", "/"), ("/\\evil.example", "/"),
    ("/login", "/"), ("/auth/logout", "/"), ("/ok\r\nSet-Cookie: x", "/"),
])
def test_after_sign_in_only_pages_here(target, expected):
    assert safe_next(target) == expected


def test_failed_sign_ins_are_throttled_for_a_while():
    now = [0.0]
    throttle = LoginThrottle(limit=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert not throttle.blocked("k")
        throttle.failed("k")
    assert throttle.blocked("k") and not throttle.blocked("other")
    now[0] = 61
    assert not throttle.blocked("k")
    throttle.failed("k")
    throttle.succeeded("k")
    assert not throttle.blocked("k")


# ── Supabase's REST API ──────────────────────────────────────────────────────────────────
def test_signing_in(supabase):
    session = supabase.auth().sign_in(ADMIN, PASSWORD)
    assert session.user.email == ADMIN and session.expires_in == 3600
    assert session.refresh_token in supabase.refresh_tokens


def test_a_wrong_password_says_nothing_more(supabase):
    with pytest.raises(AuthError) as caught:
        supabase.auth().sign_in(ADMIN, "wrong")
    assert (caught.value.status, caught.value.message) == (401, BAD_LOGIN)


def test_supabase_trouble_is_an_error_not_a_crash(supabase):
    supabase.down = True
    with pytest.raises(AuthError) as caught:
        supabase.auth().sign_in(ADMIN, PASSWORD)
    assert caught.value.status == 503
    supabase.down, supabase.rate_limited = False, True
    with pytest.raises(AuthError) as caught:
        supabase.auth().sign_in(ADMIN, PASSWORD)
    assert caught.value.status == 429


def test_a_wrong_project_key_is_reported_as_set_up_trouble(supabase):
    with pytest.raises(AuthError) as caught:
        supabase.auth(anon_key="not-the-key").sign_in(ADMIN, PASSWORD)
    assert caught.value.status == 503 and "SUPABASE_ANON_KEY" in caught.value.message


def test_refresh_tokens_work_once(supabase):
    auth = supabase.auth()
    first = auth.sign_in(ADMIN, PASSWORD)
    second = auth.refresh(first.refresh_token)
    assert second.refresh_token != first.refresh_token
    with pytest.raises(AuthError):
        auth.refresh(first.refresh_token)


def test_signing_out_never_fails(supabase):
    auth = supabase.auth()
    auth.sign_out("whatever")
    supabase.down = True
    auth.sign_out("whatever")      # unreachable: logged, the caller clears its cookies anyway
    assert supabase.signed_out == ["whatever"]


# ── the access-token check ───────────────────────────────────────────────────────────────
def test_a_token_from_supabases_signing_keys(supabase):
    user = supabase.auth().verify(supabase.token())
    assert (user.id, user.email) == (f"id-{ADMIN}", ADMIN)


def test_a_token_from_a_legacy_project():
    legacy = FakeSupabase(signing="HS256")
    assert legacy.auth().verify(legacy.token()).email == ADMIN
    # without the secret configured, an HS256 token can't be checked, so it's refused
    unconfigured = SupabaseAuth(URL, ANON, "", transport=httpx.MockTransport(legacy.handle))
    with pytest.raises(AuthError):
        unconfigured.verify(legacy.token())


@pytest.mark.parametrize("change", [
    {"expires_in": -120},                       # expired, past the 30 s leeway
    {"aud": "anon"},                            # not a signed-in user's token
    {"iss": "https://other.supabase.co/auth/v1"},
    {"key": "stranger"},                        # signed with a key Supabase never published
])
def test_tokens_that_dont_check_out(supabase, change):
    if change.get("key") == "stranger":
        change = {"key": ec.generate_private_key(ec.SECP256R1())}
    with pytest.raises(AuthError):
        supabase.auth().verify(supabase.token(**change))


def test_tokens_signed_some_other_way(supabase):
    claims = jwt.decode(supabase.token(), options={"verify_signature": False})
    unsigned = jwt.encode(claims, None, algorithm="none")
    guessed = jwt.encode(claims, "a-guessed-secret-that-is-32-bytes-long", algorithm="HS256")
    for token in (unsigned, guessed, "not.a.jwt", ""):
        with pytest.raises(AuthError):
            supabase.auth().verify(token)


def test_the_signing_keys_are_fetched_once_then_cached(supabase):
    now = [1000.0]
    auth = supabase.auth(clock=lambda: now[0])
    for _ in range(3):
        auth.verify(supabase.token())
    assert supabase.calls.count("/.well-known/jwks.json") == 1
    # a token with an unknown key id fetches the keys again, at most once a minute
    unknown = jwt.encode({"sub": "x"}, supabase.key, algorithm="ES256", headers={"kid": "new"})
    for _ in range(3):
        with pytest.raises(AuthError):
            auth.verify(unknown)
    assert supabase.calls.count("/.well-known/jwks.json") == 1
    now[0] += 61
    with pytest.raises(AuthError):
        auth.verify(unknown)
    assert supabase.calls.count("/.well-known/jwks.json") == 2


# ── the dashboard ────────────────────────────────────────────────────────────────────────
def run_one_pr(store, queue):
    queue.enqueue("analyze", "octo/app", 7, SHA, 11)
    Worker(store, queue, PolicyBook(), FakeAnalyzer(), FakeAccess(FakeGateway(make_pull()))
           ).run_once()


def test_every_page_needs_a_sign_in(store, queue, supabase):
    run_one_pr(store, queue)
    client = dashboard(store, supabase)
    for path, target in [("/", "/login?next=%2F"), ("/prs/1", "/login?next=%2Fprs%2F1"),
                         ("/audit?repo=octo%2Fapp", "/login?next=%2Faudit%3Frepo%3Docto%252Fapp")]:
        response = client.get(path)
        assert (response.status_code, response.headers["location"]) == (303, target), path
        assert response.headers["content-security-policy"] == SECURITY_HEADERS[
            "Content-Security-Policy"]
    # the page asked for comes back exactly, query and all
    login_page = client.get("/login?next=%2Faudit%3Frepo%3Docto%252Fapp")
    assert 'name="next" value="/audit?repo=octo%2Fapp"' in login_page.text
    assert client.get("/auth/me").status_code == 401
    assert client.get("/healthz").json() == {"ok": True}       # nothing more without a sign-in
    assert client.get("/static/style.css").status_code == 200
    page = client.get("/login")
    assert page.status_code == 200 and 'action="/auth/login"' in page.text
    assert 'aria-label="Main"' not in page.text                 # no way in but the form
    assert "<script" not in page.text and not re.search(r"\sstyle=", page.text)


def test_signing_in_and_out(store, queue, supabase):
    run_one_pr(store, queue)
    client = dashboard(store, supabase)
    response = sign_in(client, next_page="/prs/1")
    assert (response.status_code, response.headers["location"]) == (303, "/prs/1")
    cookies = cookie_headers(response)
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        header = cookies[name].lower()
        assert "httponly" in header and "samesite=lax" in header and "path=/" in header
        assert "secure" not in header      # plain-http localhost: browsers would drop it
    page = client.get("/prs/1")
    assert page.status_code == 200
    assert ADMIN in page.text and 'action="/auth/logout"' in page.text
    assert client.get("/auth/me").json() == {"id": f"id-{ADMIN}", "email": ADMIN}
    assert client.get("/login").headers["location"] == "/"     # already in

    access = client.cookies[ACCESS_COOKIE]
    response = client.post("/auth/logout")
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert supabase.signed_out == [access]
    assert all("max-age=0" in h.lower() for h in cookie_headers(response).values())
    assert client.get("/").status_code == 303


def test_a_wrong_password(store, supabase):
    client = dashboard(store, supabase)
    response = sign_in(client, password="wrong")
    assert response.status_code == 401
    assert BAD_LOGIN.split(",")[0] in response.text.replace("&#39;", "'")
    assert f'value="{ADMIN}"' in response.text and "wrong" not in response.text
    assert not response.headers.get_list("set-cookie")


def test_an_address_off_the_allowlist_never_reaches_supabase(store, supabase):
    response = sign_in(dashboard(store, supabase), email=OUTSIDER, password="hunter22")
    assert response.status_code == 401
    assert "/token" not in supabase.calls


def test_taking_someone_off_the_allowlist_locks_them_out(store, supabase):
    first = dashboard(store, supabase)
    sign_in(first)
    later = dashboard(store, supabase, admins="someone-else@example.com")
    later.cookies = first.cookies
    response = later.get("/")
    assert response.status_code == 303
    assert all("max-age=0" in h.lower() for h in cookie_headers(response).values())


def test_an_expired_access_token_is_renewed(store, supabase):
    client = dashboard(store, supabase)
    client.cookies.set(ACCESS_COOKIE, supabase.token(expires_in=-3600))
    client.cookies.set(REFRESH_COOKIE, supabase.session(ADMIN)["refresh_token"])
    response = client.get("/")
    assert response.status_code == 200
    assert supabase.calls.count("/token") == 1
    renewed = cookie_headers(response)
    assert ACCESS_COOKIE in renewed and REFRESH_COOKIE in renewed


def test_a_dead_session_is_cleared(store, supabase):
    client = dashboard(store, supabase)
    client.cookies.set(ACCESS_COOKIE, supabase.token(expires_in=-3600))
    client.cookies.set(REFRESH_COOKIE, "refresh-revoked")
    response = client.get("/")
    assert response.status_code == 303
    assert all("max-age=0" in h.lower() for h in cookie_headers(response).values())


def test_json_clients(store, supabase):
    client = dashboard(store, supabase)
    response = client.post("/auth/login", json={"email": ADMIN, "password": PASSWORD})
    assert response.status_code == 200
    tokens = response.json()
    assert tokens["user"]["email"] == ADMIN and tokens["token_type"] == "bearer"
    client.cookies.clear()
    bearer = {"authorization": f"Bearer {tokens['access_token']}"}
    assert client.get("/auth/me", headers=bearer).json()["email"] == ADMIN
    renewed = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert renewed.status_code == 200 and renewed.json()["refresh_token"] != tokens["refresh_token"]
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
                       ).status_code == 401
    assert client.post("/auth/logout", json={}, headers=bearer).status_code == 204
    wrong = client.post("/auth/login", json={"email": ADMIN, "password": "nope"})
    assert (wrong.status_code, wrong.json()) == (401, {"error": BAD_LOGIN})


def test_cross_site_posts_are_refused(store, supabase):
    client = dashboard(store, supabase)
    assert sign_in(client, origin="https://evil.example").status_code == 403
    assert sign_in(client, origin="http://localhost:9999").status_code == 403   # another port
    assert sign_in(client, origin="null").status_code == 403     # sandboxed or redirected
    assert sign_in(client, origin="http://localhost:8001").status_code == 303


def test_the_sign_in_form_gets_a_real_origin(store, supabase):
    # under Referrer-Policy: no-referrer a browser sends "Origin: null" with a form's POST, and
    # the sign-in form would be refused as cross-site; same-origin keeps the Origin
    page = dashboard(store, supabase).get("/login")
    assert page.headers["referrer-policy"] == "same-origin"


def test_the_dashboard_is_still_read_only(store, supabase):
    client = dashboard(store, supabase)
    sign_in(client)
    for path in ("/", "/audit", "/auth/me"):
        assert client.post(path).status_code == 405
    assert client.delete("/auth/login").status_code == 405


def test_too_many_wrong_passwords(store, supabase):
    client = dashboard(store, supabase, throttle=LoginThrottle(limit=3))
    for _ in range(3):
        assert sign_in(client, password="wrong").status_code == 401
    response = sign_in(client)          # even the right password, until the window passes
    assert response.status_code == 429 and "Too many failed sign-ins" in response.text


def test_supabase_down_shows_on_the_login_page(store, supabase):
    supabase.down = True
    response = sign_in(dashboard(store, supabase))
    assert response.status_code == 503 and "reach Supabase" in response.text


def test_other_host_names_need_https(store, supabase):
    client = dashboard(store, supabase, hosts={"review.example.com"},
                       base_url="https://review.example.com")
    assert client.get("/").status_code == 303
    response = sign_in(client)
    assert all("secure" in h.lower() for h in cookie_headers(response).values())
    assert client.get("/").status_code == 200
    other = dashboard(store, supabase, hosts={"review.example.com"},
                      base_url="https://evil.example")
    assert other.get("/login").status_code == 403


def test_without_login_the_dashboard_is_as_before(store):
    client = TestClient(create_dashboard_app(Store(store.path, readonly=True), PolicyBook()),
                        base_url="http://localhost:8001", follow_redirects=False)
    assert client.get("/").status_code == 200
    page = client.get("/login")
    assert page.status_code == 200 and "Login isn't set up" in page.text.replace("&#39;", "'")
    assert client.post("/auth/login", data={"email": ADMIN, "password": PASSWORD}
                       ).status_code == 405
    assert client.get("/auth/me").status_code == 404


# ── settings ─────────────────────────────────────────────────────────────────────────────
def test_login_is_off_until_supabase_is_set():
    settings = AppSettings()
    assert not settings.login_enabled and settings.login_problems() == [] and not settings.login()
    exposed = AppSettings(review_app_dashboard_hosts="review.example.com")
    assert exposed.login_problems() == [
        "REVIEW_APP_DASHBOARD_HOSTS needs dashboard login (SUPABASE_URL and the rest)"]


def test_half_set_up_login_is_reported():
    problems = AppSettings(supabase_url="http://proj.supabase.co",
                           review_app_dashboard_hosts="ok.example.com, bad_host!").login_problems()
    assert any("must be https" in p for p in problems)
    assert "SUPABASE_ANON_KEY is not set" in problems
    assert any("REVIEW_APP_ADMIN_EMAILS is empty" in p for p in problems)
    assert any("'bad_host!'" in p for p in problems)
    local = AppSettings(supabase_url="http://127.0.0.1:54321", supabase_anon_key="k",
                        review_app_admin_emails=ADMIN)
    assert local.login_problems() == []      # the Supabase CLI's local stack


def test_a_complete_login():
    settings = AppSettings(supabase_url=URL, supabase_anon_key=ANON,
                           review_app_admin_emails=f"{ADMIN}, @team.example",
                           review_app_dashboard_hosts="Review.Example.com")
    assert settings.login_problems() == []
    login = settings.login()
    assert login is not None and login.auth.issuer == ISSUER
    assert login.allowlist.allows("x@team.example") and login.hosts == {"review.example.com"}
    assert ANON not in repr(settings)
