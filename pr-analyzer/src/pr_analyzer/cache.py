"""On-disk cache for what doesn't change once fetched or computed.

Each entry is a text value under a kind and a key made of the parts that identify it:

    contents  (repo, commit sha, path)                     a file's text; fixed at a commit
    llm       (provider, model, ..., system prompt, prompt)   an LLM reply to that exact prompt

Entries live at <dir>/v1/<kind>/<2 hex digits>/<sha256 of the key>, as zlib-compressed JSON. They
are written to a temporary file and renamed into place, so a reader sees a whole entry or none;
an entry that can't be read or decoded counts as a miss. Entries unused for CACHE_MAX_AGE_DAYS
expire, and once the cache outgrows CACHE_MAX_SIZE_MB the least recently used go first.

Nothing that could hold a credential is written: callers never cache errors, and a value that
contains one of our own keys, or looks like a well-known token or private key, is skipped.
"""
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import structlog

from pr_analyzer.config import settings

log = structlog.get_logger(__name__)

# bump when the entry format or a key's meaning changes; old entries then simply stop matching
FORMAT_VERSION = "v1"
MEGABYTE = 1024 * 1024
DAY_SECONDS = 24 * 60 * 60
DEFAULT_MAX_SIZE_MB = 500
DEFAULT_MAX_AGE_DAYS = 30
# checking the size and age limits walks every entry, so it runs at most this often
PRUNE_INTERVAL_SECONDS = 60 * 60
PRUNE_STAMP = ".last-prune"
TEMP_PREFIX = ".tmp-"
# shorter values can't be real keys, and matching them would keep almost anything out
MIN_SECRET_LENGTH = 8

# tokens and keys a file or an LLM reply might contain; such values never reach the disk
GITHUB_TOKEN_FORMAT = r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}"
OPENAI_ANTHROPIC_KEY_FORMAT = r"sk-[A-Za-z0-9_-]{20,}"
AWS_KEY_ID_FORMAT = r"AKIA[0-9A-Z]{16}"
PRIVATE_KEY_HEADER = r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
CREDENTIAL = re.compile("|".join(
    (GITHUB_TOKEN_FORMAT, OPENAI_ANTHROPIC_KEY_FORMAT, AWS_KEY_ID_FORMAT, PRIVATE_KEY_HEADER)
))


def default_cache_dir() -> Path:
    # the XDG base directory spec says to ignore a relative XDG_CACHE_HOME
    base = os.environ.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "pr-analyzer"


def cache_key(kind: str, parts: Iterable[str]) -> str:
    # a JSON list keeps ("a/b", "c") and ("a", "b/c") apart
    encoded = json.dumps([FORMAT_VERSION, kind, *parts])
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


class DiskCache:
    """Safe to share between threads: every entry is its own file, and only the counters are
    shared state."""

    def __init__(
        self,
        root: Path | None,
        max_bytes: int = DEFAULT_MAX_SIZE_MB * MEGABYTE,
        max_age_seconds: float = DEFAULT_MAX_AGE_DAYS * DAY_SECONDS,
        secrets: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
    ):
        self.root = root / FORMAT_VERSION if root is not None else None
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self.secrets = tuple(s for s in secrets if len(s) >= MIN_SECRET_LENGTH)
        self.clock = clock
        self._lock = threading.Lock()
        self.hits = self.misses = self.writes = 0
        self.writable = True   # until a write fails: then the run carries on without writing

    @classmethod
    def disabled(cls) -> "DiskCache":
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def get(self, kind: str, *parts: str) -> str | None:
        if self.root is None:
            return None
        value = self._read(self._path(kind, parts))
        with self._lock:
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
        return value

    def put(self, kind: str, *parts: str, value: str) -> None:
        if self.root is None or not self.writable or not self.storable(value):
            return
        path = self._path(kind, parts)
        data = zlib.compress(json.dumps({"value": value}).encode("ascii"))
        try:
            self._make_root()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(path, data, mtime=self.clock())
        except OSError as e:
            # a cache that can't be written must never fail the analysis, nor log every file
            with self._lock:
                first, self.writable = self.writable, False
            if first:
                log.warning("cache_write_failed", error=str(e), hint="caching off for this run")
            return
        with self._lock:
            self.writes += 1

    def storable(self, value: str) -> bool:
        return not CREDENTIAL.search(value) and not any(s in value for s in self.secrets)

    def close(self) -> None:
        if self.root is None:
            return
        log.info("cache_used", hits=self.hits, misses=self.misses, writes=self.writes)
        if self.writes and self._prune_due():
            self.prune()

    def prune(self) -> None:
        """Drop expired entries, then the least recently used until the cache fits its size."""
        if self.root is None or not self.root.is_dir():
            return
        now = self.clock()
        entries = []
        for path, stat in self._entries():
            age = now - stat.st_mtime
            if age > self.max_age_seconds:
                discard(path)
            elif not path.name.startswith(TEMP_PREFIX):
                entries.append((stat.st_mtime, stat.st_size, path))
            elif age > PRUNE_INTERVAL_SECONDS:
                discard(path)   # left behind by a writer that crashed

        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            discard(path)
            total -= size
        with contextlib.suppress(OSError):
            stamp = self.root / PRUNE_STAMP
            stamp.touch()
            os.utime(stamp, (now, now))

    def _path(self, kind: str, parts: tuple[str, ...]) -> Path:
        assert self.root is not None
        key = cache_key(kind, parts)
        return self.root / kind / key[:2] / key

    def _read(self, path: Path) -> str | None:
        now = self.clock()
        try:
            with open(path, "rb") as handle:
                expired = now - os.fstat(handle.fileno()).st_mtime > self.max_age_seconds
                data = b"" if expired else handle.read()
        except OSError:
            return None   # not cached yet, or unreadable
        if expired:
            return None   # prune() deletes it
        try:
            value = json.loads(zlib.decompress(data))["value"]
        except (zlib.error, ValueError, TypeError, KeyError):
            value = None
        if not isinstance(value, str):
            log.warning("cache_entry_corrupt", path=str(path))
            discard(path)
            return None
        # a hit counts as a use: past the size limit, the least recently used entries go first
        with contextlib.suppress(OSError):
            os.utime(path, (now, now))
        return value

    def _make_root(self) -> None:
        assert self.root is not None
        if not self.root.is_dir():
            # private: the cache holds file contents, which may come from private repositories
            self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.mkdir(mode=0o700, exist_ok=True)

    def _prune_due(self) -> bool:
        assert self.root is not None
        try:
            last = (self.root / PRUNE_STAMP).stat().st_mtime
        except OSError:
            return True
        return self.clock() - last > PRUNE_INTERVAL_SECONDS

    def _entries(self) -> Iterator[tuple[Path, os.stat_result]]:
        assert self.root is not None
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                if name == PRUNE_STAMP:
                    continue
                path = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    yield path, path.stat()


def write_atomically(path: Path, data: bytes, mtime: float) -> None:
    # write a temporary file next to the entry and rename it over the entry: readers never see
    # half an entry, and a crash leaves only a temporary file, which prune() removes later
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.utime(temp, (mtime, mtime))
        os.replace(temp, path)
    except BaseException:
        discard(Path(temp))
        raise


def discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def open_cache(enabled: bool = True) -> DiskCache:
    """The cache the settings describe, or a disabled one (CACHE_ENABLED=false or --no-cache)."""
    if not (enabled and settings.cache_enabled):
        return DiskCache.disabled()
    root = Path(settings.cache_dir).expanduser() if settings.cache_dir else default_cache_dir()
    return DiskCache(
        root,
        max_bytes=settings.cache_max_size_mb * MEGABYTE,
        max_age_seconds=settings.cache_max_age_days * DAY_SECONDS,
        secrets=(settings.github_token, settings.openai_api_key, settings.anthropic_api_key),
    )
