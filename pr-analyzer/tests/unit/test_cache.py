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


def overwrite(path, data: bytes, mtime: float = NOW) -> None:
    # keep the entry's age on the test clock; writing sets it to the real time
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def another_user(monkeypatch) -> None:
    # stands in for directories and files that someone else made: pr-analyzer now runs as a user
    # who owns none of them
    monkeypatch.setattr(cache_module, "current_uid", lambda: os.getuid() + 1)


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

# ── directories and files that aren't the user's ──────────────────────────────
def test_symlinked_cache_folder_is_neither_used_nor_pruned(tmp_path):
    # <CACHE_DIR>/v1 made a link to the user's own files: prune() used to walk and delete them
    victim = tmp_path / "victim"
    (victim / "photos").mkdir(parents=True)
    overwrite(victim / "notes.txt", b"written long ago", mtime=NOW - 40 * DAY)
    overwrite(victim / "photos" / "big.raw", b"x" * 3000)
    (tmp_path / "cache").mkdir(mode=0o700)
    (tmp_path / "cache" / "v1").symlink_to(victim, target_is_directory=True)
    cache = make_cache(tmp_path, max_bytes=1000)

    with capture_logs() as logs:
        cache.put("contents", "k", value="v")
        assert cache.get("contents", "k") is None
        cache.prune()
        cache.close()

    assert sorted(p.relative_to(victim).as_posix() for p in victim.rglob("*")) == [
        "notes.txt", "photos", "photos/big.raw"]
    assert not cache.enabled
    assert [(entry["event"], entry["problem"]) for entry in logs] == [
        ("cache_dir_unsafe", "it is a symbolic link")]   # once, not per call

def test_symlinked_cache_dir_is_not_used(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "cache").symlink_to(elsewhere, target_is_directory=True)
    cache = make_cache(tmp_path)

    cache.put("contents", "k", value="v")

    assert not cache.enabled
    assert list(elsewhere.iterdir()) == []

def test_cache_dir_of_another_user_is_not_used(tmp_path, monkeypatch):
    # entries planted under keys anyone can work out, for public repositories
    make_cache(tmp_path).put("contents", "k", value="planted")
    another_user(monkeypatch)
    cache = make_cache(tmp_path)

    with capture_logs() as logs:
        assert cache.get("contents", "k") is None
        cache.put("contents", "other", value="v")

    assert not cache.enabled
    assert len(entry_files(cache)) == 1
    assert [(entry["event"], entry["problem"]) for entry in logs] == [
        ("cache_dir_unsafe", "it belongs to another user")]

def test_cache_dirs_others_can_open_are_made_private(tmp_path):
    root = tmp_path / "cache" / "v1"
    root.mkdir(parents=True)
    os.chmod(root.parent, 0o777)
    os.chmod(root, 0o755)
    cache = make_cache(tmp_path)

    cache.put("contents", "k", value="v")

    assert (mode(root.parent), mode(root)) == (0o700, 0o700)
    assert cache.get("contents", "k") == "v"

def test_cache_dir_that_cannot_be_made_private_is_not_used(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    os.chmod(tmp_path / "cache", 0o777)
    monkeypatch.setattr(cache_module.os, "chmod", lambda path, mode: None)   # FAT drives ignore it
    cache = make_cache(tmp_path)

    cache.put("contents", "k", value="v")

    assert not cache.enabled
    assert list((tmp_path / "cache").iterdir()) == []

def test_entry_of_another_user_is_a_miss(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value="planted")   # the directories pass their check here
    another_user(monkeypatch)

    assert cache.get("contents", "k") is None

def test_entry_is_never_read_through_a_symbolic_link(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value="kept somewhere else")
    [entry] = entry_files(cache)
    elsewhere = tmp_path / "elsewhere"
    entry.rename(elsewhere)
    entry.symlink_to(elsewhere)

    assert cache.get("contents", "k") is None
    assert elsewhere.exists()

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
    "ASIA" + "E" * 16,
    "xoxb-" + "123456789012-" + "f" * 24,
    "sk_" + "live_" + "g" * 24,
    "rk_" + "live_" + "h" * 24,
    "AIza" + "i" * 35,
    "glpat-" + "j" * 20,
    "hf_" + "k" * 34,
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
    "PuTTY-User-" + "Key-File-3: ssh-ed25519",
])
def test_values_containing_credentials_are_not_written(tmp_path, credential):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value=f"config = load()\nkey = '{credential}'\n")

    assert cache.get("contents", "k") is None
    assert entry_files(cache) == []

@pytest.mark.parametrize("text", [
    "Slack bot tokens start with xoxb-",
    "stripe.api_key = os.environ['STRIPE_KEY']   # sk_live_... in production",
    "-----BEGIN " + "PGP PUBLIC KEY BLOCK-----",
    "-----BEGIN " + "CERTIFICATE-----",
])
def test_lookalikes_are_still_cached(tmp_path, text):
    cache = make_cache(tmp_path)
    cache.put("contents", "k", value=text)
    assert cache.get("contents", "k") == text

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

def test_prune_deletes_only_the_caches_own_files(tmp_path):
    # a CACHE_DIR whose v1 folder already held files (an API's v1/ folder, say)
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock, max_age_seconds=DAY)
    cache.put("contents", "k", value="v")
    [entry] = entry_files(cache)
    bucket = entry.parent.name
    other_bucket = format((int(bucket, 16) + 1) % 256, "02x")
    strangers = [
        cache.root / "notes.txt",
        cache.root / "users" / "list.py",
        entry.parent / "README",
        entry.parent / (other_bucket + "0" * 62),        # an entry's name, in the wrong bucket
        cache.root / "contents" / "zz" / (bucket + "0" * 62),   # a bucket that can't be one
    ]
    for path in strangers:
        path.parent.mkdir(exist_ok=True)
        overwrite(path, b"not the cache's")
    clock.now += 2 * DAY

    cache.prune()

    assert not entry.exists()
    assert [path for path in strangers if not path.exists()] == []

def test_prune_follows_no_symbolic_link_inside_the_cache(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, clock=clock, max_age_seconds=DAY)
    cache.put("contents", "k", value="v")
    [entry] = entry_files(cache)
    bucket = entry.parent.name
    # the user's own files, laid out like a kind of entry: <bucket>/<entry name>
    victim = tmp_path / "victim"
    (victim / bucket).mkdir(parents=True)
    target = victim / bucket / (bucket + "0" * 62)
    overwrite(target, b"the user's file")
    (cache.root / "linked-kind").symlink_to(victim, target_is_directory=True)
    (cache.root / "llm").mkdir()
    (cache.root / "llm" / bucket).symlink_to(victim / bucket, target_is_directory=True)
    linked_entry = entry.parent / (bucket + "1" * 62)
    linked_entry.symlink_to(target)
    clock.now += 2 * DAY

    cache.prune()

    assert not entry.exists()
    assert target.exists() and linked_entry.is_symlink()

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
