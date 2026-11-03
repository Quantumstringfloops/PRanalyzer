import functools
import gzip
import importlib.util
import io
import json
import sys
import threading
import types
from email.utils import formatdate
from pathlib import Path

import github
import pytest
from github import GithubException, RateLimitExceededException
from structlog.testing import capture_logs
from typer.testing import CliRunner
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from pr_analyzer import cli
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
README = Path(__file__).resolve().parents[2] / "README.md"
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
    """threading.Event, except that wait() returns at once, as if its timeout had run out: the
    timeout is recorded and the fake monotonic clock moves on by that much. No test sleeps."""

    def __init__(self, clock):
        self.clock = clock
        self.flag = False

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag

    def wait(self, timeout=None):
        with self.clock.lock:   # the workers of a parallel fetch wait at the same time
            self.clock.waits.append(timeout)
            self.clock.mono += timeout
        return self.flag


class BlockingEvent(FakeEvent):
    """An event whose wait() lasts until set(), however long its timeout: a rate-limit wait
    that only cancel() ends."""

    def __init__(self, clock):
        super().__init__(clock)
        self.released = threading.Event()
        self.waiting = threading.Semaphore(0)   # released as each wait() begins

    def set(self):
        super().set()
        self.released.set()

    def wait(self, timeout=None):
        self.waiting.release()
        if not self.released.wait(TIMEOUT):
            self.released.set()   # a test gone wrong: from here on the waits run their course
        return super().wait(timeout)


class Clock:
    """The wall and monotonic clocks a pause reads, and the events it waits on."""

    def __init__(self, event=FakeEvent):
        self.now = NOW       # the wall clock, which GitHub's reset times are read against
        self.mono = 500.0    # the monotonic clock, which the waits are measured on
        self.waits = []      # the timeout of every Event.wait, in order
        self.events = []
        self.event_class = event
        self.lock = threading.Lock()

    def event(self):
        event = self.event_class(self)
        self.events.append(event)
        return event

    def pause(self):
        return RateLimitPause(clock=lambda: self.now, monotonic=lambda: self.mono,
                              new_event=self.event)

    @property
    def waited(self):
        """The seconds spent waiting, in all."""
        return sum(self.waits)


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
    ({"Retry-After": "soon"}, 60),
    # Retry-After can also be an HTTP date
    ({"Retry-After": formatdate(NOW + 30, usegmt=True)}, 30),
    ({"Retry-After": formatdate(NOW + 7200, usegmt=True)}, 7200),
    ({"Retry-After": "Fri, 15 Jan 2027 08:00:30 -0000"}, 30),   # -0000: UTC
    ({"Retry-After": formatdate(NOW - 600, usegmt=True)}, 0),
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

    assert clock.waited == 21

def test_secondary_limit_named_only_in_the_message_waits_a_minute(retry, clock):
    again = retry.increment("GET", "/repos/o/r", response=response(403, body=SECONDARY, compressed=True))
    again.sleep()
    assert clock.waited == 60

def test_429_honours_retry_after(retry, clock):
    again = retry.increment("POST", "/repos/o/r/pulls/1/reviews",
                            response=response(429, {"Retry-After": "7"}))
    again.sleep()
    assert clock.waited == 7

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

def test_http_date_over_the_cap_raises_at_once(retry, clock):
    # it was read as "GitHub didn't say", and waited out three times for a minute
    date = formatdate(NOW + 7200, usegmt=True)
    with pytest.raises(RateLimitExceededException) as excinfo:
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": date}))
    assert excinfo.value.retry_at == NOW + 7200
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
    with pytest.raises(RateLimitExceededException) as excinfo:
        retry.increment("GET", "/repos/o/r", response=response(429, {"Retry-After": "1"}))
    assert clock.waited == 1 + 1 + 1
    assert excinfo.value.retry_at == NOW + 1

def test_server_errors_back_off_without_the_rate_limit_pause(retry, clock):
    again = retry.increment("GET", "/repos/o/r", response=response(503, {"Retry-After": "3600"}))
    again.sleep()   # the first retry follows at once; a Retry-After of an hour is not honoured
    assert clock.waits == []

def test_other_threads_hold_back_while_one_waits(clock):
    pause = clock.pause()

    pause.wait(30)                         # one worker waits out a limit, until 530
    clock.mono = 510
    pause.wait_if_paused()                 # another worker, ten seconds into that wait
    assert clock.mono == 530
    clock.mono += 5
    pause.wait_if_paused()                 # the limit has reset

    assert clock.waited == 30 + 20

def test_setting_the_system_clock_back_does_not_stretch_a_wait(clock):
    pause = clock.pause()

    pause.wait(30)                         # until 530
    clock.now -= 3600                      # an hour back, ten seconds into the wait
    clock.mono = 510
    pause.wait_if_paused()

    assert clock.waited == 30 + 20

def test_waits_are_logged_as_warnings(clock):
    # the default log level is WARNING, and a run that stands still for minutes must say why
    with capture_logs() as logs:
        clock.pause().wait(30, status=429, url="/repos/o/r", attempt=1)

    assert logs == [{"event": "github_rate_limit_wait", "log_level": "warning", "seconds": 30,
                     "status": 429, "url": "/repos/o/r", "attempt": 1}]

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

    assert clock.waited == 2

def test_each_fetch_shares_the_runs_waits_but_is_cancelled_on_its_own(clock):
    run = clock.pause()
    fetch = run.scope()

    fetch.wait(30)                         # a worker of the fetch waits out a limit, until 530
    clock.mono = 510                       # ten seconds into that wait, the fetch ends
    fetch.cancel()
    with pytest.raises(RequestsCancelled):
        fetch.wait_if_paused()             # its other workers stop at once
    run.wait_if_paused()                   # the client's later requests still hold back

    assert clock.waited == 30 + 20

# ── Ctrl-C on the main thread ─────────────────────────────────────────────────
class WindowsEvent(FakeEvent):
    """An event as on Windows with Python 3.11-3.13: Ctrl-C can't cut its wait() short, so the
    KeyboardInterrupt comes only once the wait returns."""

    ctrl_c = 512.3   # when Ctrl-C is pressed, on the monotonic clock: 12.3 s into a wait

    def wait(self, timeout=None):
        cancelled = super().wait(timeout)
        if self.clock.mono >= self.ctrl_c:
            raise KeyboardInterrupt
        return cancelled


class LateEvent(FakeEvent):
    """An event whose wait() returns a quarter of a second late, as on a busy machine."""

    def wait(self, timeout=None):
        self.clock.mono += 0.25
        return super().wait(timeout)


def test_waits_block_half_a_second_at_a_time(clock):
    clock.pause().wait(60.25)

    assert clock.waits == [0.5] * 120 + [0.25]

def test_ctrl_c_on_windows_ends_a_wait_within_half_a_second():
    # time.sleep stops at Ctrl-C there, an Event wait doesn't: a minute's wait on the main thread
    # (one file to fetch, the review to post) held Ctrl-C back for the whole minute
    clock = Clock(event=WindowsEvent)

    with pytest.raises(KeyboardInterrupt):
        clock.pause().wait(60)

    assert WindowsEvent.ctrl_c <= clock.mono <= WindowsEvent.ctrl_c + 0.5

def test_slices_that_end_late_do_not_stretch_the_wait():
    # each one is measured against the deadline on the monotonic clock, not counted out
    clock = Clock(event=LateEvent)

    clock.pause().wait(60)

    assert clock.mono == 560

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
    assert clock.waited >= 6   # b.py's worker may have held back meanwhile, on the same clock
    assert api.sent("/repos/o/r/contents/a.py") == 2

def test_fetch_reports_a_limit_that_resets_too_late(api, clock):
    reset = int(NOW) + 3600
    api.add("/repos/o/r/pulls/1", (403, {"X-RateLimit-Remaining": "0",
                                         "X-RateLimit-Reset": str(reset)}, PRIMARY))
    client = GithubClient(token="", rate_limits=clock.pause())

    with pytest.raises(RateLimitError) as excinfo:
        client.fetch_pr(URL)

    assert excinfo.value.reset_time.timestamp() == reset + 1   # the reset is in whole seconds
    assert clock.waits == []

@pytest.mark.parametrize("retry_after, reset_in", [
    # GitHub's "wait a minute", not the reset of the hourly limit 40 minutes away
    (None, 60),
    (formatdate(NOW + 7200, usegmt=True), 7200),   # was "now"
])
def test_limit_too_long_to_wait_says_when_to_try_again(api, clock, monkeypatch,
                                                      retry_after, reset_in):
    monkeypatch.setattr(settings, "github_rate_limit_max_wait_seconds", 30)
    headers = {"X-RateLimit-Remaining": "4000", "X-RateLimit-Reset": str(int(NOW) + 2400)}
    if retry_after:
        headers["Retry-After"] = retry_after
    api.add("/repos/o/r/pulls/1", (403, headers, SECONDARY))
    client = GithubClient(token="", rate_limits=clock.pause())

    with pytest.raises(RateLimitError) as excinfo:
        client.fetch_pr(URL)

    assert excinfo.value.reset_time.timestamp() == NOW + reset_in
    assert api.sent("/repos/o/r/pulls/1") == 1 and clock.waits == []

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
    assert clock.waits == [0.5, 0.5]   # each worker's 30 s wait, ended in its first slice

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

    with pytest.raises(RateLimitError) as excinfo:
        GithubClient(token="", rate_limits=clock.pause()).fetch_pr(URL)
    join_threads("github-contents")

    assert excinfo.value.reset_time.timestamp() == NOW + 3600
    assert api.sent("/repos/o/r/contents/a.py") == 1
    assert clock.waits == [0.5]   # a.py's 30 s wait, ended in its first slice

def test_client_carries_on_after_a_failed_fetch(api, clock):
    # the failed fetch cancels its own waits, not the client's
    api.add("/repos/o/r/contents/b.py", (429, {"Retry-After": "3600"}, SLOW_DOWN), content("b.py"))
    client = GithubClient(token="", rate_limits=clock.pause())
    with pytest.raises(RateLimitError):
        client.fetch_pr(URL)

    pr = client.fetch_pr(URL)

    assert [f.raw_content for f in pr.files] == ["x = 1\n", "x = 1\n"]

# ── a limit while posting the review ──────────────────────────────────────────
def test_limit_while_posting_is_a_warning_as_the_readme_says(api, monkeypatch):
    # only a limit while the PR is fetched fails the run (exit code 2); when posting the review
    # is limited, the report still stands and the exit code follows the verdict
    monkeypatch.setattr(settings, "github_token", "test-token")
    monkeypatch.setattr(settings, "github_rate_limit_max_wait_seconds", 60)
    api.add("/repos/o/r/contents/.pr-analyzer.toml", (404, {}, {"message": "Not Found"}))
    api.add("/repos/o/r/commits/abc", (200, {}, {"sha": "abc", "url": f"{API}/repos/o/r/commits/abc"}))
    api.add("/repos/o/r/pulls/1/comments", (200, {}, []))
    api.add("/repos/o/r/pulls/1/reviews", (200, {}, []))
    api.add("/repos/o/r/pulls/1/reviews", (429, {"Retry-After": "120"}, SLOW_DOWN), method="POST")

    result = CliRunner().invoke(cli.app, ["analyze", URL, "--skip-llm", "--post-review",
                                          "--no-cache", "-o", "json"])

    assert result.exit_code == 0
    assert any(w.startswith("Could not post the GitHub review: Rate limit exceeded")
               for w in json.loads(result.stdout)["warnings"])
    assert api.requests.count(("POST", "/repos/o/r/pulls/1/reviews")) == 1
    readme = README.read_text(encoding="utf-8")
    setting = next(line for line in readme.splitlines()
                   if line.startswith("| `GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS`"))
    section = next(part for part in readme.split("\n\n") if part.startswith("**Rate limits.**"))
    for text in (setting, section):
        assert "exit code 2" in text and "review is posted" in text and "warning" in text
