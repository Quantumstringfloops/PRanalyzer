import threading
from concurrent.futures import as_completed

import pytest

from pr_analyzer.cache import DiskCache
from pr_analyzer.config import settings
from pr_analyzer.llm import review as review_module
from pr_analyzer.llm.chunking import ReviewRequest

REPLY = '{"suggestions": []}'
# a test that goes wrong waits this long for another thread, rather than hanging
TIMEOUT = 5


def parts(count):
    return [ReviewRequest(prompt=f"part {i}", filenames=[f"f{i}.py"]) for i in range(count)]


def key(prompt):
    return ("test", prompt)


class Provider:
    """An LLM that answers at once, except for the prompts in `slow`, which stay in flight until
    finish() is called, and `interrupt`, during which Ctrl-C comes on the calling thread."""

    def __init__(self, slow=(), interrupt=None):
        self.slow = set(slow)
        self.interrupt = interrupt
        self.prompts = []
        self.threads = set()
        self.lock = threading.Lock()
        self.in_flight = threading.Semaphore(0)   # released as each slow prompt is sent
        self.done = threading.Event()

    def ask(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
            self.threads.add(threading.current_thread())
        if prompt == self.interrupt:
            raise KeyboardInterrupt
        if prompt in self.slow:
            self.in_flight.release()
            self.done.wait(TIMEOUT)
        return REPLY

    def finish(self):
        self.done.set()
        for thread in self.threads - {threading.current_thread()}:
            thread.join(TIMEOUT)


def ctrl_c_after(provider, replies, in_flight):
    """as_completed, until Ctrl-C comes: after `replies` replies, once `in_flight` more requests
    have been sent. Ctrl-C reaches only the main thread, which waits in as_completed."""
    def interrupted(futures):
        done = as_completed(futures)
        for _ in range(replies):
            yield next(done)
        for _ in range(in_flight):
            assert provider.in_flight.acquire(timeout=TIMEOUT)
        raise KeyboardInterrupt
    return interrupted


# ── Ctrl-C ────────────────────────────────────────────────────────────────────
def test_ctrl_c_sends_no_part_still_queued(monkeypatch):
    # with LLM_MAX_CONCURRENCY above 1, every queued part used to be sent, and paid for, before
    # the interrupt came through
    provider = Provider(slow={"part 2", "part 3"})
    monkeypatch.setattr(review_module, "as_completed", ctrl_c_after(provider, 2, in_flight=2))

    with pytest.raises(KeyboardInterrupt):
        review_module.send(parts(8), list(range(8)), provider.ask, workers=2)

    assert sorted(provider.prompts) == ["part 0", "part 1", "part 2", "part 3"]
    assert all(t.is_alive() for t in provider.threads)   # the parts in flight aren't waited for
    provider.finish()
    assert len(provider.prompts) == 4   # and nothing more is sent once they are done

@pytest.mark.parametrize("workers", [1, 2])
def test_replies_already_in_are_cached_when_ctrl_c_stops_the_review(monkeypatch, tmp_path,
                                                                     workers):
    # they were paid for, and a re-run used to send them all again
    monkeypatch.setattr(settings, "llm_token_budget", 0)
    monkeypatch.setattr(settings, "llm_max_concurrency", workers)
    cache = DiskCache(tmp_path)
    if workers == 1:
        provider = Provider(interrupt="part 2")
    else:
        provider = Provider(slow={"part 2", "part 3"})
        monkeypatch.setattr(review_module, "as_completed", ctrl_c_after(provider, 2, in_flight=2))

    with pytest.raises(KeyboardInterrupt):
        review_module.review(parts(6), provider.ask, cache, key, keep=lambda reply: True)
    provider.finish()

    assert [cache.get("llm", *key(f"part {i}")) for i in range(4)] == [REPLY, REPLY, None, None]
