"""Sending a PR's LLM review requests: through the cache, within the run's token budget.

A request whose reply is cached costs nothing and is always answered. The others are sent in PR
order while LLM_TOKEN_BUDGET allows (estimated prompt tokens, system prompt included); the first
one that would go over it is skipped, and so is every uncached request after it. When a request
fails, the ones not yet sent are dropped too, but the replies already in are kept; only if none
came in does the error propagate, as a single request's always did. The warnings say how many
files weren't AI-reviewed. Ctrl-C stops the review at once: no request still queued is sent, and
the replies already in are cached before the interrupt goes on.
"""
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import structlog

from pr_analyzer.cache import DiskCache
from pr_analyzer.config import settings
from pr_analyzer.llm.chunking import ReviewRequest, estimate_tokens
from pr_analyzer.llm.prompts import SYSTEM_PROMPT

log = structlog.get_logger(__name__)

# waits for parallel requests come in slices: on Windows one long wait holds Ctrl-C back until it ends
WAIT_SLICE_SECONDS = 0.5


@dataclass
class ReviewPlan:
    replies: dict[int, str] = field(default_factory=dict)   # request index -> reply
    keys: dict[int, tuple[str, ...]] = field(default_factory=dict)
    to_send: list[int] = field(default_factory=list)
    over_budget: list[int] = field(default_factory=list)
    tokens: int = 0


def plan(
    requests: list[ReviewRequest],
    cache: DiskCache,
    cache_key: Callable[[str], tuple[str, ...]],
    budget: int,
) -> ReviewPlan:
    result = ReviewPlan()
    for index, request in enumerate(requests):
        if cache.enabled:
            result.keys[index] = cache_key(request.prompt)
            cached = cache.get("llm", *result.keys[index])
            if cached is not None:
                result.replies[index] = cached
                continue
        cost = estimate_tokens(SYSTEM_PROMPT + request.prompt)
        if result.over_budget or (budget and result.tokens + cost > budget):
            result.over_budget.append(index)
        else:
            result.tokens += cost
            result.to_send.append(index)
    return result


def send(
    requests: list[ReviewRequest],
    indexes: list[int],
    ask: Callable[[str], str],
    workers: int,
    replies: dict[int, str] | None = None,
) -> tuple[dict[int, str], Exception | None]:
    """Replies by request index, and the first error; after an error nothing new is sent.

    The replies go into `replies` as they come in, so a caller that Ctrl-C interrupts still has
    the ones already received."""
    replies = {} if replies is None else replies
    if workers <= 1 or len(indexes) <= 1:
        for index in indexes:
            try:
                replies[index] = ask(requests[index].prompt)
            except Exception as e:
                return replies, e
        return replies, None

    error: Exception | None = None
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-review")
    try:
        futures: dict[Future[str], int] = {
            pool.submit(ask, requests[index].prompt): index for index in indexes
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=WAIT_SLICE_SECONDS, return_when=FIRST_COMPLETED)
            for future in done:
                if future.cancelled():
                    continue
                try:
                    replies[futures[future]] = future.result()
                except Exception as e:
                    error = error or e
                    for queued in pending:
                        queued.cancel()   # only those not started yet; the others finish
    except BaseException:
        # Ctrl-C: the requests still queued are dropped rather than sent (each one is paid for),
        # and the ones in flight aren't waited for
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()   # every request is done: the workers are idle
    return replies, error


def not_reviewed(requests: list[ReviewRequest], indexes: list[int]) -> int:
    return len({name for index in indexes for name in requests[index].filenames})


def review(
    requests: list[ReviewRequest],
    ask: Callable[[str], str],
    cache: DiskCache,
    cache_key: Callable[[str], tuple[str, ...]],
    keep: Callable[[str], bool],
) -> tuple[list[str], list[str]]:
    """Return (replies in request order, warnings)."""
    budget = settings.llm_token_budget
    work = plan(requests, cache, cache_key, budget)
    log.info("llm_requests", total=len(requests), cached=len(work.replies),
             sending=len(work.to_send), over_budget=len(work.over_budget), tokens=work.tokens)

    sent: dict[int, str] = {}
    try:
        sent, error = send(requests, work.to_send, ask, settings.llm_max_concurrency, sent)
    finally:
        # also when Ctrl-C ends the run part way: the replies already in were paid for
        for index, reply in sent.items():
            if cache.enabled and keep(reply):
                cache.put("llm", *work.keys[index], value=reply)
    work.replies.update(sent)
    if error is not None and not work.replies:
        raise error

    total = not_reviewed(requests, list(range(len(requests))))
    warnings = []
    if work.over_budget:
        count = not_reviewed(requests, work.over_budget)
        warnings.append(f"{count} of {total} file(s) were not AI-reviewed, or only in part: "
                        f"reviewing them would have gone over LLM_TOKEN_BUDGET ({budget} tokens)")
    unsent = [index for index in work.to_send if index not in sent]
    if error is not None:
        count = not_reviewed(requests, unsent)
        warnings.append(f"{count} of {total} file(s) were not AI-reviewed, or only in part: "
                        f"an LLM request failed ({error})")
    return [work.replies[index] for index in sorted(work.replies)], warnings
