"""Waiting out GitHub's rate limits instead of failing the run.

GitHub refuses a rate-limited request with 403 or 429 and doesn't carry it out, so the request,
even a POST, can be sent again once the limit allows:

- primary limit (`x-ratelimit-remaining: 0`): wait until `x-ratelimit-reset` (epoch seconds)
- secondary limit: wait `retry-after` seconds, or a minute when GitHub doesn't say

A wait longer than GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS raises RateLimitExceededException instead,
which the client reports as RateLimitError (exit code 2), as before.

This is a urllib3 Retry of our own rather than PyGithub's GithubRetry because GithubRetry (2.10)
caps only primary-limit waits: a 403 with Retry-After falls through to urllib3, which sleeps for
whatever it says (up to six hours, silently). It ignores 429, retries 5xx errors on POST too (a
502 after GitHub created a review would post the review twice), and logs through the standard
logging module, which this project doesn't show. The plain urllib3 Retry used before had the
same silent Retry-After sleep on 429 and 503 responses.
"""
import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Self

import structlog
from github import RateLimitExceededException
from github.Requester import Requester
from urllib3.connectionpool import ConnectionPool
from urllib3.exceptions import MaxRetryError
from urllib3.response import BaseHTTPResponse
from urllib3.util.retry import Retry

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


def rate_limit_wait(headers: Mapping[str, str], now: float) -> float:
    """Seconds GitHub asks for before a rate-limited request may be sent again."""
    headers = lowercase(headers)
    retry_after = number(headers.get("retry-after"))
    if retry_after is not None:
        return max(retry_after, 0.0)
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


class RateLimitPause:
    """The rate-limit waits of one run, shared by all its GitHub clients and threads: while one
    request waits for a limit to reset, the others hold back instead of adding to it."""

    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()
        self._until = 0.0

    def wait(self, seconds: float, **context: Any) -> None:
        with self._lock:
            now = self.clock()
            self._until = max(self._until, now + seconds)
            remaining = self._until - now
        log.info("github_rate_limit_wait", seconds=round(remaining, 1), **context)
        self.sleep(remaining)

    def wait_if_paused(self) -> None:
        with self._lock:
            remaining = self._until - self.clock()
        if remaining > 0:
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

        wait = rate_limit_wait(headers, self.pause.clock())
        if wait > self.max_wait:
            raise RateLimitExceededException(response.status, data, headers)
        try:
            retry = super().increment(method, url, response, error, _pool, _stacktrace)
        except MaxRetryError:
            raise RateLimitExceededException(response.status, data, headers) from None
        retry.rate_limit_wait = wait
        return retry

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        if self.rate_limit_wait is None:
            super().sleep(response)
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
