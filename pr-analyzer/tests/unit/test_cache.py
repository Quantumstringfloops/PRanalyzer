import os
import stat
import zlib

import pytest
from structlog.testing import capture_logs

from pr_analyzer import cache as cache_module
from pr_analyzer.cache import DiskCache, default_cache_dir, open_cache

NOW = 1_800_000_000.0
DAY = 24 * 60 * 60


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_cache(tmp_path, clock=None, **kwargs) -> DiskCache:
    return DiskCache(tmp_path / "cache", clock=clock or Clock(), **kwargs)


def entry_files(cache: DiskCache):
    return sorted(p for p in cache.root.rglob("*") if p.is_file() and p.name != ".last-prune")


def overwrite(path, data: bytes) -> None:
    # keep the entry's age on the test clock; writing sets it to the real time
    path.write_bytes(data)
    os.utime(path, (NOW, NOW))


# ── reading and writing ───────────────────────────────────────────────────────
def test_miss_then_hit(tmp_path):
    cache = make_cache(tmp_path)

    assert cache.get("contents", "o/r", "abc", "app.py") is None
    cache.put("contents", "o/r", "abc", "app.py", value="x = 1\n")

    assert cache.get("contents", "o/r", "abc", "app.py") == "x = 1\n"
    assert (cache.hits, cache.misses, cache.writes) == (1, 1, 1)

def test_key_parts_and_kinds_are_kept_apart(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("contents", "a/b", "c", value="one")

    assert cache.get("contents", "a", "b/c") is None
    assert cache.get("llm", "a/b", "c") is None

def test_any_text_round_trips(tmp_path):
    cache = make_cache(tmp_path)
    text = 'naïve — 日本語 \x00 "quoted" \ud800'
    cache.put("llm", "k", value=text)
    assert cache.get("llm", "k") == text

def test_writes_leave_no_temporary_files(tmp_path):
    cache = make_cache(tmp_path)
    for i in range(5):
        cache.put("contents", str(i), value="v")

    assert len(entry_files(cache)) == 5
    assert not any(p.name.startswith(".tmp-") for p in cache.root.rglob("*"))

def test_cache_directory_is_private(tmp_path):
    # file contents may come from private repositories
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value="v")

    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700

# ── damaged entries ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("data", [
    b"",
    b"not zlib at all",
    zlib.compress(b"not json"),
    zlib.compress(b'{"other": "v"}'),
    zlib.compress(b'{"value": 3}'),
    zlib.compress(b'["value"]'),
    zlib.compress(b'{"value": "v"}')[:-3],   # cut short
])
def test_corrupt_entry_is_a_miss_and_is_removed(tmp_path, data):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value="v")
    [path] = entry_files(cache)
    overwrite(path, data)

    assert cache.get("contents", "k") is None
    assert not path.exists()

def test_unreadable_entry_is_a_miss(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value="v")
    [path] = entry_files(cache)
    path.unlink()
    path.mkdir()   # opening it now fails

    assert cache.get("contents", "k") is None

def test_unwritable_cache_never_fails_the_run(tmp_path):
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the cache directory should be")
    cache = DiskCache(blocker)

    with capture_logs() as logs:
        for name in ("a", "b", "c"):
            cache.put("contents", name, value="v")

    assert cache.get("contents", "a") is None
    assert (cache.writes, cache.writable) == (0, False)
    assert [entry["event"] for entry in logs] == ["cache_write_failed"]   # once, not per file

# ── what is never cached ──────────────────────────────────────────────────────
@pytest.mark.parametrize("credential", [
    "ghp_" + "a" * 36,
    "github_pat_" + "b" * 30,
    "sk-" + "c" * 30,
    "AKIA" + "D" * 16,
    "-----BEGIN " + "RSA PRIVATE KEY-----",
])
def test_values_containing_credentials_are_not_written(tmp_path, credential):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value=f"config = load()\nkey = '{credential}'\n")

    assert cache.get("contents", "k") is None
    assert entry_files(cache) == []

def test_our_own_keys_are_never_written(tmp_path):
    cache = make_cache(tmp_path, secrets=["an-unusual-own-key-value", ""])
    cache.put("llm", "k", value="the model repeated an-unusual-own-key-value here")
    assert cache.get("llm", "k") is None

def test_short_settings_values_do_not_block_caching(tmp_path):
    cache = make_cache(tmp_path, secrets=["t"])
    cache.put("contents", "k", value="text")
    assert cache.get("contents", "k") == "text"

# ── age and size limits ───────────────────────────────────────────────────────
def test_expired_entry_is_a_miss(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock, max_age_seconds=DAY)
    cache.put("contents", "k", value="v")

    clock.now += 2 * DAY

    assert cache.get("contents", "k") is None

def test_a_hit_keeps_an_entry_fresh(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock, max_age_seconds=DAY)
    cache.put("contents", "k", value="v")

    clock.now += 0.75 * DAY
    assert cache.get("contents", "k") == "v"
    clock.now += 0.75 * DAY
    assert cache.get("contents", "k") == "v"

def test_prune_removes_expired_entries(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock, max_age_seconds=DAY)
    cache.put("contents", "old", value="v")
    clock.now += 2 * DAY
    cache.put("contents", "new", value="v")

    cache.prune()

    assert len(entry_files(cache)) == 1
    assert cache.get("contents", "new") == "v"

def test_prune_drops_least_recently_used_beyond_the_size_limit(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock)
    for name in ("a", "b", "c"):
        cache.put("llm", name, value=name * 1000)
        clock.now += 60
    assert cache.get("llm", "a") == "a" * 1000   # now the most recently used
    sizes = {p.stat().st_size for p in entry_files(cache)}
    assert len(sizes) == 1
    cache.max_bytes = 2 * sizes.pop()

    cache.prune()

    assert [cache.get("llm", name) is not None for name in ("a", "b", "c")] == [True, False, True]

def test_prune_removes_abandoned_temporary_files(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock)
    cache.put("contents", "k", value="v")
    [entry] = entry_files(cache)
    abandoned = entry.parent / ".tmp-abandoned"
    overwrite(abandoned, b"half an entry")
    clock.now += 2 * 60 * 60

    cache.prune()

    assert not abandoned.exists()
    assert entry.exists()

def test_close_prunes_after_writes_at_most_hourly(tmp_path, monkeypatch):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock)
    pruned = []
    monkeypatch.setattr(cache, "prune", lambda: pruned.append(clock.now))

    cache.close()                                   # nothing written yet
    cache.put("contents", "k", value="v")
    cache.close()                                   # first prune
    overwrite(cache.root / ".last-prune", b"")
    cache.close()                                   # pruned less than an hour ago
    clock.now += 2 * 60 * 60
    cache.close()

    assert pruned == [NOW, NOW + 2 * 60 * 60]

# ── settings ──────────────────────────────────────────────────────────────────
def test_default_dir_follows_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "pr-analyzer"

def test_default_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/dir")   # the spec says to ignore it
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / ".cache" / "pr-analyzer"

def test_open_cache_follows_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module.settings, "cache_enabled", True)
    monkeypatch.setattr(cache_module.settings, "cache_dir", str(tmp_path / "c"))
    monkeypatch.setattr(cache_module.settings, "cache_max_size_mb", 2)
    monkeypatch.setattr(cache_module.settings, "cache_max_age_days", 3)

    cache = open_cache()

    assert cache.root == tmp_path / "c" / "v1"
    assert (cache.max_bytes, cache.max_age_seconds) == (2 * 1024 * 1024, 3 * DAY)
    assert not (tmp_path / "c").exists()   # created on the first write only

def test_cache_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module.settings, "cache_enabled", True)
    assert not open_cache(enabled=False).enabled          # --no-cache
    monkeypatch.setattr(cache_module.settings, "cache_enabled", False)
    assert not open_cache().enabled                       # CACHE_ENABLED=false

def test_disabled_cache_does_nothing():
    cache = DiskCache.disabled()
    cache.put("contents", "k", value="v")
    cache.prune()
    cache.close()
    assert cache.get("contents", "k") is None
