"""Waiting out GitHub's rate limits instead of failing the run.

GitHub refuses a rate-limited request with 403 or 429 and doesn't carry it out, so the request,
even a POST, can be sent again once the limit allows:

- primary limit (`x-ratelimit-remaining: 0`): wait until `x-ratelimit-reset` (epoch seconds)
- secondary limit: wait `retry-after` (seconds, or an HTTP date), or a minute when GitHub
  doesn't say

A wait longer than GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS raises RateLimitExceededException instead,
which the client reports as RateLimitError, with the time GitHub lets the request through again:
while the PR is fetched that stops the run (exit code 2), while the review is posted it becomes
a warning. Waits are logged as warnings, and they can be cancelled: when a parallel fetch fails
or is interrupted (Ctrl-C), its waits end at once and none of its requests is sent again.

This is a urllib3 Retry of our own rather than PyGithub's GithubRetry because GithubRetry (2.10)
caps only primary-limit waits: a 403 with Retry-After falls through to urllib3, which sleeps for
whatever it says (up to six hours, silently). It ignores 429, retries 5xx errors on POST too (a
502 after GitHub created a review would post the review twice), and logs through the standard
logging module, which this project doesn't show. The plain urllib3 Retry used before had the
same silent Retry-After sleep on 429 and 503 responses.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import structlog
from github import RateLimitExceededException
from github.Requester import Requester
from urllib3.exceptions import MaxRetryError
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    # annotations only: BaseHTTPResponse is new in urllib3 2, and PyGithub and requests still
    # allow urllib3 1.26
    from urllib3.connectionpool import ConnectionPool
    from urllib3.response import BaseHTTPResponse

log = structlog.get_logger(__name__)

FORBIDDEN = 403
TOO_MANY_REQUESTS = 429
RATE_LIMIT_STATUSES = frozenset({FORBIDDEN, TOO_MANY_REQUESTS})
# GitHub: without a retry-after header, "wait for at least one minute before retrying"
SECONDARY_LIMIT_WAIT_SECONDS = 60.0
DEFAULT_MAX_WAIT_SECONDS = 60.0
# transient server errors, retried on idempotent requests only
SERVER_ERRORS = (500, 502, 503, 504)
MAX_RETRIES = 3
# The longest a wait blocks at a time. On Windows, Python 3.11-3.13 can't interrupt an Event wait
# the way it interrupts time.sleep (bpo-29971): Ctrl-C raises KeyboardInterrupt on the main thread
# only once the wait returns, which in slices this long is within half a second. Elsewhere Ctrl-C
# still cuts a slice short at once.
WAIT_SLICE_SECONDS = 0.5


def lowercase(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def has_limit_headers(headers: Mapping[str, str]) -> bool:
    return "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"


def number(value: str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def retry_after(value: str | None, now: float) -> float | None:
    """A Retry-After header in seconds from `now`: it gives either seconds or an HTTP date."""
    seconds = number(value)
    if seconds is None and value:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:   # "-0000": UTC, says RFC 5322
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, OverflowError):
            return None
    return max(seconds, 0.0) if seconds is not None else None


def rate_limit_wait(headers: Mapping[str, str], now: float) -> float:
    """Seconds GitHub asks for before a rate-limited request may be sent again."""
    headers = lowercase(headers)
    after = retry_after(headers.get("retry-after"), now)
    if after is not None:
        return after
    reset = number(headers.get("x-ratelimit-reset"))
    if headers.get("x-ratelimit-remaining") == "0" and reset is not None:
        # +1: the reset time is in whole seconds, and when in that second it comes is unknown
        return max(reset - now, 0.0) + 1
    return SECONDARY_LIMIT_WAIT_SECONDS


def read_json(response: BaseHTTPResponse) -> Any:
    # parsed the way PyGithub parses an error body, so the exception raised from it matches
    text = response.read(decode_content=True).decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"data": text}


class RateLimitExceeded(RateLimitExceededException):
    """A rate limit that isn't waited out, and when GitHub lets the request through again."""

    def __init__(self, status: int, data: Any, headers: dict[str, str], retry_at: float):
        super().__init__(status, data, headers)
        self.retry_at = retry_at   # epoch seconds


class RequestsCancelled(Exception):
    """Raised instead of waiting, retrying or sending a request once its fetch is cancelled."""


class _Deadline:
    """When the run's requests may go on, on the monotonic clock: shared by a pause and its
    scopes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.at = 0.0


class RateLimitPause:
    """The rate-limit waits of one run, shared by all its GitHub clients and threads: while one
    request waits for a limit to reset, the others hold back instead of adding to it.

    Waits are on an Event rather than time.sleep, so cancel() ends them at once: Ctrl-C reaches
    only the main thread, and nothing else could stop a worker thread's wait. They block for at
    most WAIT_SLICE_SECONDS at a time, so that Ctrl-C on the main thread isn't held back on
    Windows. Deadlines are on the monotonic clock, which a change to the system time doesn't
    move; `clock` is the wall clock that GitHub's reset times are read against."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        new_event: Callable[[], threading.Event] = threading.Event,
    ):
        self.clock = clock
        self.monotonic = monotonic
        self.new_event = new_event
        self.cancelled = new_event()
        self._resume = _Deadline()

    def scope(self) -> RateLimitPause:
        """A pause with this one's waits but a cancel() of its own: one per parallel fetch, so
        that ending the fetch stops its requests, not the client's later ones."""
        scoped = RateLimitPause(self.clock, self.monotonic, self.new_event)
        scoped._resume = self._resume
        return scoped

    def cancel(self) -> None:
        """End every wait at once; from then on each wait, retry and request raises."""
        self.cancelled.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise RequestsCancelled("the GitHub requests were cancelled")

    def sleep(self, seconds: float) -> None:
        """Wait `seconds` without holding back other requests, unless cancel() comes first."""
        if seconds > 0 and not self.cancelled.is_set():
            until = self.monotonic() + seconds
            remaining = seconds
            # a slice at a time, each measured against the deadline, so no lateness adds up
            while remaining > 0 and not self.cancelled.wait(min(remaining, WAIT_SLICE_SECONDS)):
                remaining = until - self.monotonic()
        self.raise_if_cancelled()

    def wait(self, seconds: float, **context: Any) -> None:
        with self._resume.lock:
            now = self.monotonic()
            self._resume.at = max(self._resume.at, now + seconds)
            remaining = self._resume.at - now
        # a warning, so the default log level shows it: the run can stand still for minutes
        log.warning("github_rate_limit_wait", seconds=round(remaining, 1), **context)
        self.sleep(remaining)

    def wait_if_paused(self) -> None:
        with self._resume.lock:
            remaining = self._resume.at - self.monotonic()
        self.sleep(remaining)


class RateLimitRetry(Retry):
    """urllib3's retry policy for GitHub requests: transient server errors on idempotent requests,
    as before, plus rate limits on any request, waited out for up to `max_wait` seconds."""

    def __init__(
        self,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        pause: RateLimitPause | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.max_wait = max_wait
        self.pause = pause if pause is not None else RateLimitPause()
        self.rate_limit_wait: float | None = None   # set on the copy that increment() returns

    def new(self, **kw: Any) -> Self:
        kw.setdefault("max_wait", self.max_wait)
        kw.setdefault("pause", self.pause)
        return super().new(**kw)

    def is_retry(self, method: str, status_code: int, has_retry_after: bool = False) -> bool:
        if status_code in RATE_LIMIT_STATUSES:
            return True   # increment() tells a rate limit from a refusal
        return super().is_retry(method, status_code, has_retry_after)

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: BaseHTTPResponse | None = None,
        error: Exception | None = None,
        _pool: ConnectionPool | None = None,
        _stacktrace: TracebackType | None = None,
    ) -> Self:
        self.pause.raise_if_cancelled()   # a cancelled fetch sends nothing again, whatever failed
        if response is None or response.status not in RATE_LIMIT_STATUSES:
            return super().increment(method, url, response, error, _pool, _stacktrace)

        headers = lowercase(response.headers)
        data = None
        if response.status == FORBIDDEN and not has_limit_headers(headers):
            # without the headers, only the message tells a secondary limit from "forbidden".
            # Reading it uses up the body, so a refusal is raised here, as PyGithub would raise it
            data = read_json(response)
            message = data.get("message") if isinstance(data, dict) else None
            if not Requester.isRateLimitError(message or ""):
                raise Requester.createException(response.status, headers, data)

        now = self.pause.clock()
        wait = rate_limit_wait(headers, now)
        if wait > self.max_wait:
            raise RateLimitExceeded(response.status, data, headers, retry_at=now + wait)
        try:
            retry = super().increment(method, url, response, error, _pool, _stacktrace)
        except MaxRetryError:
            raise RateLimitExceeded(response.status, data, headers, retry_at=now + wait) from None
        retry.rate_limit_wait = wait
        return retry

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        if self.rate_limit_wait is None:
            # the back-off before retrying a server error or a lost connection (github_retry has
            # urllib3 ignore their Retry-After), which cancel() cuts short too
            self.pause.sleep(self.get_backoff_time())
            return
        last = self.history[-1] if self.history else None
        self.pause.wait(
            self.rate_limit_wait,
            status=last.status if last else None,
            url=last.url if last else None,
            attempt=len(self.history),
        )


def github_retry(max_wait: float, pause: RateLimitPause | None = None) -> RateLimitRetry:
    return RateLimitRetry(
        max_wait=max_wait,
        pause=pause,
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=SERVER_ERRORS,
        raise_on_status=False,
        # urllib3 would otherwise sleep out a Retry-After on a 429 or 503 itself: for up to six
        # hours, without a word, and past GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS
        respect_retry_after_header=False,
    )
