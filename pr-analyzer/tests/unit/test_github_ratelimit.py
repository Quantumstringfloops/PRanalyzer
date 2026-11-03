import functools
import gzip
import io
import json
import threading

import github
import pytest
from github import GithubException, RateLimitExceededException
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from pr_analyzer.exceptions import RateLimitError
from pr_analyzer.github import client as client_module
from pr_analyzer.github.client import GithubClient
from pr_analyzer.github.ratelimit import RateLimitPause, github_retry, rate_limit_wait

NOW = 1_800_000_000.0
API = "http://api.github.test"
SECONDARY = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes "
                        "before you try again."}
PRIMARY = {"message": "API rate limit exceeded for 203.0.113.7."}
FORBIDDEN = {"message": "Resource not accessible by integration"}


def response(status, headers=None, body=None, compressed=False):
    data = json.dumps(body).encode() if body is not None else b""
    headers = dict(headers or {})
    if compressed:
        data = gzip.compress(data)
        headers["Content-Encoding"] = "gzip"
    # preload_content=False, as requests asks for: the body is still unread
    return HTTPResponse(body=io.BytesIO(data), headers=headers, status=status,
                        preload_content=False, decode_content=False)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def retry(sleeps):
    return github_retry(60, RateLimitPause(sleep=sleeps.append, clock=lambda: NOW))


# ── how long to wait ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("headers, expected", [
    ({"Retry-After": "30"}, 30),
    ({"retry-after": "0"}, 0),
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 20)}, 21),
    ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) - 5)}, 1),   # clock skew
    # every response carries the primary reset; it only counts once the limit is used up
    ({"X-RateLimit-Remaining": "12", "X-RateLimit-Reset": str(int(NOW) + 3000)}, 60),
    ({}, 60),                                   # GitHub: wait at least a minute
    ({"Retry-After": "nan"}, 60),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 60),
])
def test_rate_limit_wait(headers, expected):
    assert rate_limit_wait(headers, NOW) == expected

# ── which responses are retried ───────────────────────────────────────────────
@pytest.mark.parametrize("method, status, retried", [
    ("GET", 502, True),
    ("POST", 502, False),    # GitHub may have created the review before the proxy failed
    ("PATCH", 503, False),
    ("POST", 403, True),     # a rate-limited request is never carried out, so resending is safe
    ("GET", 429, True),
    ("GET", 404, False),
])
def test_what_is_retried(retry, method, status, retried):
    assert retry.is_retry(method, status) is retried

def test_primary_limit_waits_until_the_reset(retry, sleeps):
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 20)}

    again = retry.increment("GET", "/repos/o/r", response=response(403, headers, PRIMARY))
    again.sleep()

    assert sleeps == [21]

def test_secondary_limit_named_only_in_the_message_waits_a_minute(retry, sleeps):
    again = retry.increment("GET", "/repos/o/r", response=response(403, body=SECONDARY, compressed=True))
    again.sleep()
    assert sleeps == [60]

def test_429_honours_retry_after(retry, sleeps):
    again = retry.increment("POST", "/repos/o/r/pulls/1/reviews",
                            response=response(429, {"Retry-After": "7"}))
    again.sleep()
    assert sleeps == [7]

def test_refusal_is_not_retried_and_keeps_its_message(retry, sleeps):
    with pytest.raises(GithubException) as excinfo:
        retry.increment("GET", "/repos/o/r", response=response(403, body=FORBIDDEN, compressed=True))

    assert not isinstance(excinfo.value, RateLimitExceededException)
    assert (excinfo.value.status, excinfo.value.data) == (403, FORBIDDEN)
    assert sleeps == []

def test_wait_over_the_cap_raises_at_once(retry, sleeps):
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "61"}))
    assert sleeps == []

def test_zero_cap_never_waits(sleeps):
    retry = github_retry(0, RateLimitPause(sleep=sleeps.append, clock=lambda: NOW))
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(403, {"Retry-After": "1"}, SECONDARY))

def test_gives_up_after_three_retries(retry, sleeps):
    for _ in range(3):
        retry = retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "1"}))
        retry.sleep()
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "1"}))
    assert sleeps == [1, 1, 1]

def test_server_errors_back_off_without_the_rate_limit_pause(retry, sleeps):
    again = retry.increment("GET", "/repos/o/r", response=response(503, {"Retry-After": "3600"}))
    again.sleep()   # the first retry follows at once; a Retry-After of an hour is not honoured
    assert sleeps == []

def test_other_threads_hold_back_while_one_waits():
    clock = [NOW]
    sleeps = []
    pause = RateLimitPause(sleep=sleeps.append, clock=lambda: clock[0])

    pause.wait(30)
    clock[0] += 10
    pause.wait_if_paused()                 # another worker, ten seconds later
    clock[0] += 25
    pause.wait_if_paused()                 # the limit has reset

    assert sleeps == [30, 20]

# ── through PyGithub, requests and urllib3 ────────────────────────────────────
class FakeAPI:
    """Answers urllib3's requests from canned responses: no sockets, no network."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.lock = threading.Lock()

    def add(self, path, *answers):
        self.routes[path] = list(answers)

    def make_request(self, pool, conn, method, url, **kwargs):
        path = url.split("?")[0]
        with self.lock:
            self.requests.append((method, path))
            answers = self.routes[path]
            status, headers, body = answers.pop(0) if len(answers) > 1 else answers[0]
        return HTTPResponse(
            body=io.BytesIO(json.dumps(body).encode()),
            headers={"Content-Type": "application/json", **headers},
            status=status,
            preload_content=kwargs.get("preload_content", True),
            decode_content=kwargs.get("decode_content", True),
            request_method=method,
            request_url=url,
        )


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(HTTPConnectionPool, "_make_request",
                        lambda pool, conn, method, url, **kwargs:
                            fake.make_request(pool, conn, method, url, **kwargs))
    # every client talks to the fake API, without PyGithub's 0.25 s pause between requests
    monkeypatch.setattr(client_module, "Github", functools.partial(
        github.Github, base_url=API, seconds_between_requests=0, seconds_between_writes=0))
    fake.add("/repos/o/r", (200, {}, {"id": 1, "full_name": "o/r", "url": f"{API}/repos/o/r"}))
    fake.add("/repos/o/r/pulls/1", (200, {}, {
        "number": 1, "title": "t", "user": {"login": "neil"}, "base": {"ref": "main"},
        "head": {"ref": "f", "sha": "abc"}, "additions": 2, "deletions": 0, "draft": False,
        "url": f"{API}/repos/o/r/pulls/1",
    }))
    fake.add("/repos/o/r/pulls/1/files", (200, {}, [
        {"filename": name, "status": "added", "additions": 1, "deletions": 0,
         "patch": "@@ -0,0 +1 @@\n+x = 1"} for name in ("a.py", "b.py")
    ]))
    for name in ("a.py", "b.py"):
        fake.add(f"/repos/o/r/contents/{name}", content(name))
    return fake


def content(name):
    return (200, {}, {"type": "file", "encoding": "base64", "content": "eCA9IDEK", "path": name})


def test_fetch_waits_out_a_rate_limit_and_carries_on(api, sleeps):
    limited = (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 5)}, PRIMARY)
    api.add("/repos/o/r/contents/a.py", limited, content("a.py"))
    client = GithubClient(token="", rate_limits=RateLimitPause(sleep=sleeps.append, clock=lambda: NOW))

    pr = client.fetch_pr("https://github.com/o/r/pull/1")

    assert [f.raw_content for f in pr.files] == ["x = 1\n", "x = 1\n"]
    assert sleeps[0] == 6
    assert api.requests.count(("GET", "/repos/o/r/contents/a.py")) == 2

def test_fetch_reports_a_limit_that_resets_too_late(api, sleeps):
    reset = int(NOW) + 3600
    api.add("/repos/o/r/pulls/1", (403, {"X-RateLimit-Remaining": "0",
                                         "X-RateLimit-Reset": str(reset)}, PRIMARY))
    client = GithubClient(token="", rate_limits=RateLimitPause(sleep=sleeps.append, clock=lambda: NOW))

    with pytest.raises(RateLimitError) as excinfo:
        client.fetch_pr("https://github.com/o/r/pull/1")

    assert excinfo.value.reset_time.timestamp() == reset
    assert sleeps == []
