import functools
import gzip
import importlib.util
import io
import json
import sys
import threading
import types

import github
import pytest
from github import GithubException, RateLimitExceededException
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from pr_analyzer.config import settings
from pr_analyzer.exceptions import RateLimitError
from pr_analyzer.github import client as client_module
from pr_analyzer.github.client import GithubClient
from pr_analyzer.github.ratelimit import (
    RateLimitPause,
    RequestsCancelled,
    github_retry,
    rate_limit_wait,
)

NOW = 1_800_000_000.0
API = "http://api.github.test"
URL = "https://github.com/o/r/pull/1"
SECONDARY = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes "
                        "before you try again."}
PRIMARY = {"message": "API rate limit exceeded for 203.0.113.7."}
FORBIDDEN = {"message": "Resource not accessible by integration"}
SLOW_DOWN = {"message": "slow down"}
# a test that goes wrong waits this long for another thread, rather than hanging
TIMEOUT = 5


def response(status, headers=None, body=None, compressed=False):
    data = json.dumps(body).encode() if body is not None else b""
    headers = dict(headers or {})
    if compressed:
        data = gzip.compress(data)
        headers["Content-Encoding"] = "gzip"
    # preload_content=False, as requests asks for: the body is still unread
    return HTTPResponse(body=io.BytesIO(data), headers=headers, status=status,
                        preload_content=False, decode_content=False)


class FakeEvent:
    """threading.Event, except that wait() records its timeout and returns at once: no test
    sleeps."""

    def __init__(self, waits):
        self.waits = waits
        self.flag = False

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.flag


class BlockingEvent(FakeEvent):
    """An event whose wait() records its timeout and then lasts until set(), however long the
    timeout: a rate-limit wait that only cancel() ends."""

    def __init__(self, waits):
        super().__init__(waits)
        self.released = threading.Event()
        self.waiting = threading.Semaphore(0)   # released as each wait() begins

    def set(self):
        super().set()
        self.released.set()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.waiting.release()
        self.released.wait(TIMEOUT)
        return self.flag


class Clock:
    """The wall and monotonic clocks a pause reads, and the events it waits on."""

    def __init__(self, event=FakeEvent):
        self.now = NOW       # the wall clock, which GitHub's reset times are read against
        self.mono = 500.0    # the monotonic clock, which the waits are measured on
        self.waits = []      # the timeout of every wait, in order
        self.events = []
        self.event_class = event

    def event(self):
        event = self.event_class(self.waits)
        self.events.append(event)
        return event

    def pause(self):
        return RateLimitPause(clock=lambda: self.now, monotonic=lambda: self.mono,
                              new_event=self.event)


def join_threads(prefix):
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(TIMEOUT)
            assert not thread.is_alive()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def retry(clock):
    return github_retry(60, clock.pause())


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

def test_primary_limit_waits_until_the_reset(retry, clock):
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 20)}

    again = retry.increment("GET", "/repos/o/r", response=response(403, headers, PRIMARY))
    again.sleep()

    assert clock.waits == [21]

def test_secondary_limit_named_only_in_the_message_waits_a_minute(retry, clock):
    again = retry.increment("GET", "/repos/o/r", response=response(403, body=SECONDARY, compressed=True))
    again.sleep()
    assert clock.waits == [60]

def test_429_honours_retry_after(retry, clock):
    again = retry.increment("POST", "/repos/o/r/pulls/1/reviews",
                            response=response(429, {"Retry-After": "7"}))
    again.sleep()
    assert clock.waits == [7]

def test_refusal_is_not_retried_and_keeps_its_message(retry, clock):
    with pytest.raises(GithubException) as excinfo:
        retry.increment("GET", "/repos/o/r", response=response(403, body=FORBIDDEN, compressed=True))

    assert not isinstance(excinfo.value, RateLimitExceededException)
    assert (excinfo.value.status, excinfo.value.data) == (403, FORBIDDEN)
    assert clock.waits == []

def test_wait_over_the_cap_raises_at_once(retry, clock):
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "61"}))
    assert clock.waits == []

def test_zero_cap_never_waits(clock):
    retry = github_retry(0, clock.pause())
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(403, {"Retry-After": "1"}, SECONDARY))
    assert clock.waits == []

def test_gives_up_after_three_retries(retry, clock):
    for _ in range(3):
        retry = retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "1"}))
        retry.sleep()
    with pytest.raises(RateLimitExceededException):
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "1"}))
    assert clock.waits == [1, 1, 1]

def test_server_errors_back_off_without_the_rate_limit_pause(retry, clock):
    again = retry.increment("GET", "/repos/o/r", response=response(503, {"Retry-After": "3600"}))
    again.sleep()   # the first retry follows at once; a Retry-After of an hour is not honoured
    assert clock.waits == []

def test_other_threads_hold_back_while_one_waits(clock):
    pause = clock.pause()

    pause.wait(30)
    clock.mono += 10
    pause.wait_if_paused()                 # another worker, ten seconds later
    clock.mono += 25
    pause.wait_if_paused()                 # the limit has reset

    assert clock.waits == [30, 20]

def test_setting_the_system_clock_back_does_not_stretch_a_wait(clock):
    pause = clock.pause()

    pause.wait(30)
    clock.now -= 3600                      # an hour back, ten seconds into the wait
    clock.mono += 10
    pause.wait_if_paused()

    assert clock.waits == [30, 20]

# ── cancelling the waits ──────────────────────────────────────────────────────
def test_cancel_ends_the_waits_and_stops_the_retries(clock):
    pause = clock.pause()
    retry = github_retry(60, pause)
    again = retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "30"}))

    pause.cancel()

    for wait in (again.sleep, pause.wait_if_paused, lambda: pause.wait(30)):
        with pytest.raises(RequestsCancelled):
            wait()
    for failure in (response(429, {"Retry-After": "1"}), response(502)):
        with pytest.raises(RequestsCancelled):
            retry.increment("GET", "/repos/o/r", response=failure)
    assert clock.waits == []

def test_back_off_after_a_server_error_can_be_cancelled_too(clock):
    pause = clock.pause()
    retry = github_retry(60, pause)
    for _ in range(2):
        retry = retry.increment("GET", "/repos/o/r", response=response(502))

    retry.sleep()                          # urllib3 slept these with time.sleep
    pause.cancel()
    with pytest.raises(RequestsCancelled):
        retry.sleep()

    assert clock.waits == [2]

def test_each_fetch_shares_the_runs_waits_but_is_cancelled_on_its_own(clock):
    run = clock.pause()
    fetch = run.scope()

    fetch.wait(30)                         # a worker of the fetch waits out a limit
    clock.mono += 10
    run.wait_if_paused()                   # the run's next request holds back meanwhile
    fetch.cancel()
    with pytest.raises(RequestsCancelled):
        fetch.wait_if_paused()
    clock.mono += 20
    run.wait_if_paused()                   # the client's later requests are not cancelled

    assert clock.waits == [30, 20]

# ── urllib3 1.26 ──────────────────────────────────────────────────────────────
def test_imports_with_urllib3_1_26(monkeypatch):
    # PyGithub and requests still allow urllib3 1.26, which has no BaseHTTPResponse: importing
    # it broke every command
    old = types.ModuleType("urllib3.response")
    old.HTTPResponse = HTTPResponse
    monkeypatch.setitem(sys.modules, "urllib3.response", old)

    spec = importlib.util.find_spec("pr_analyzer.github.ratelimit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.github_retry(60).max_wait == 60

# ── through PyGithub, requests and urllib3 ────────────────────────────────────
class FakeAPI:
    """Answers urllib3's requests from canned responses: no sockets, no network. An answer can
    be a function, called when the request comes in."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.lock = threading.Lock()

    def add(self, path, *answers, method="GET"):
        self.routes[(method, path)] = list(answers)

    def make_request(self, pool, conn, method, url, **kwargs):
        path = url.split("?")[0]
        with self.lock:
            self.requests.append((method, path))
            answers = self.routes[(method, path)]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
        status, headers, body = answer() if callable(answer) else answer
        return HTTPResponse(
            body=io.BytesIO(json.dumps(body).encode()),
            headers={"Content-Type": "application/json", **headers},
            status=status,
            preload_content=kwargs.get("preload_content", True),
            decode_content=kwargs.get("decode_content", True),
            request_method=method,
            request_url=url,
        )

    def sent(self, path):
        return self.requests.count(("GET", path))


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
    fake.files = lambda *names: fake.add("/repos/o/r/pulls/1/files", (200, {}, [
        {"filename": name, "status": "added", "additions": 1, "deletions": 0,
         "patch": "@@ -0,0 +1 @@\n+x = 1"} for name in names
    ]))
    fake.files("a.py", "b.py")
    for name in ("a.py", "b.py"):
        fake.add(f"/repos/o/r/contents/{name}", content(name))
    return fake


def content(name):
    return (200, {}, {"type": "file", "encoding": "base64", "content": "eCA9IDEK", "path": name})


def test_fetch_waits_out_a_rate_limit_and_carries_on(api, clock):
    limited = (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW) + 5)}, PRIMARY)
    api.add("/repos/o/r/contents/a.py", limited, content("a.py"))
    client = GithubClient(token="", rate_limits=clock.pause())

    pr = client.fetch_pr(URL)

    assert [f.raw_content for f in pr.files] == ["x = 1\n", "x = 1\n"]
    assert clock.waits[0] == 6
    assert api.sent("/repos/o/r/contents/a.py") == 2

def test_fetch_reports_a_limit_that_resets_too_late(api, clock):
    reset = int(NOW) + 3600
    api.add("/repos/o/r/pulls/1", (403, {"X-RateLimit-Remaining": "0",
                                         "X-RateLimit-Reset": str(reset)}, PRIMARY))
    client = GithubClient(token="", rate_limits=clock.pause())

    with pytest.raises(RateLimitError) as excinfo:
        client.fetch_pr(URL)

    assert excinfo.value.reset_time.timestamp() == reset
    assert clock.waits == []

# ── ending a parallel fetch ───────────────────────────────────────────────────
def test_ctrl_c_ends_the_rate_limit_waits_of_a_parallel_fetch(api, monkeypatch):
    # two workers wait out a limit and two files are queued when Ctrl-C comes, which reaches
    # only the main thread: as KeyboardInterrupt while it waits for the workers
    monkeypatch.setattr(settings, "github_max_workers", 2)
    clock = Clock(event=BlockingEvent)
    api.files("a.py", "b.py", "c.py", "d.py")
    for name in ("a.py", "b.py", "c.py", "d.py"):
        api.add(f"/repos/o/r/contents/{name}", (429, {"Retry-After": "30"}, SLOW_DOWN), content(name))

    def ctrl_c(futures, return_when):
        for _ in range(2):
            assert clock.events[-1].waiting.acquire(timeout=TIMEOUT)
        raise KeyboardInterrupt

    monkeypatch.setattr(client_module, "wait", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        GithubClient(token="", rate_limits=clock.pause()).fetch_pr(URL)
    join_threads("github-contents")

    contents = [path for _, path in api.requests if "/contents/" in path]
    assert len(contents) == len(set(contents)) <= 2   # none sent again, and none of the queued
    assert {"/repos/o/r/contents/c.py", "/repos/o/r/contents/d.py"}.isdisjoint(contents)
    assert clock.waits == [30, 30]

def test_limit_too_long_for_one_worker_ends_the_others_waits(api, monkeypatch):
    # b.py's limit is over the cap while a.py waits out its own: the error used to come only
    # once a.py's waits were over and it had been sent again, up to three times
    monkeypatch.setattr(settings, "github_max_workers", 2)
    clock = Clock(event=BlockingEvent)
    b_sent = threading.Event()

    def a_limited():
        assert b_sent.wait(TIMEOUT)
        return 429, {"Retry-After": "30"}, SLOW_DOWN

    def b_too_long():
        b_sent.set()
        assert clock.events[-1].waiting.acquire(timeout=TIMEOUT)   # a.py is waiting
        return 429, {"Retry-After": "3600"}, SLOW_DOWN

    api.add("/repos/o/r/contents/a.py", a_limited, content("a.py"))
    api.add("/repos/o/r/contents/b.py", b_too_long)

    with pytest.raises(RateLimitError):
        GithubClient(token="", rate_limits=clock.pause()).fetch_pr(URL)
    join_threads("github-contents")

    assert api.sent("/repos/o/r/contents/a.py") == 1
    assert clock.waits == [30]

def test_client_carries_on_after_a_failed_fetch(api, clock):
    # the failed fetch cancels its own waits, not the client's
    api.add("/repos/o/r/contents/b.py", (429, {"Retry-After": "3600"}, SLOW_DOWN), content("b.py"))
    client = GithubClient(token="", rate_limits=clock.pause())
    with pytest.raises(RateLimitError):
        client.fetch_pr(URL)

    pr = client.fetch_pr(URL)

    assert [f.raw_content for f in pr.files] == ["x = 1\n", "x = 1\n"]
