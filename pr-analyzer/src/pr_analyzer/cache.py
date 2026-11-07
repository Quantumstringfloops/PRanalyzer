"""On-disk cache for what doesn't change once fetched or computed.

Each entry is a text value under a kind and a key made of the parts that identify it:

    contents  (repo, commit sha, path)                     a file's text; fixed at a commit
    llm       (provider, model, ..., system prompt, prompt)   an LLM reply to that exact prompt

Entries live at <dir>/v1/<kind>/<2 hex digits>/<sha256 of the key>, as zlib-compressed JSON. They
are written to a temporary file and renamed into place, so a reader sees a whole entry or none;
an entry that can't be read or decoded counts as a miss. Entries unused for CACHE_MAX_AGE_DAYS
expire, and once the cache outgrows CACHE_MAX_SIZE_MB the least recently used go first.

The cache may hold code from private repositories, and anyone else who could write to it could
plant entries, swap them, or make prune() delete files elsewhere through a symbolic link. So
<dir> and <dir>/v1 are created private to the user (PRIVATE_MODE), and before either is used it
must be a real directory (not a symbolic link) that belongs to the user: one of the user's that
others can use too is made private, and anything else turns the cache off for the run, with a
warning. Until then others could have planted, linked or swapped entries in it, so once either
has been made private the cache's own files in <dir>/v1 are deleted before any is used, with a
warning if there were any (see _clear). The <kind> and <2 hex digits> directories that hold an
entry must be real directories of the user's too, or the cache is off for the run, with a
warning. Entries are read only from the user's own regular files with no other hard link,
never through a symbolic link. prune() deletes nothing but entry and temporary files in
<kind>/<2 hex digits>/, following no symbolic link, and replaces its stamp file rather than
writing through a link put in its place.

Callers never cache errors. A value isn't written if it contains one of our own keys (`secrets`:
open_cache passes GITHUB_TOKEN, OPENAI_API_KEY and ANTHROPIC_API_KEY; values under 8 characters
don't count) or matches CREDENTIAL: a private key block (PEM, OpenSSH or PGP), a PuTTY key file,
or a GitHub, OpenAI or Anthropic (sk-), AWS access key ID (AKIA, ASIA), Slack (xox?-), Stripe live
(sk_live_, rk_live_), Google API (AIza), GitLab (glpat-) or Hugging Face (hf_) token. Other
secrets, such as an AWS secret access key on its own or a password in a database URL, aren't
recognised.
"""
import contextlib
import hashlib
import json
import os
import re
import stat
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
PRIVATE_MODE = 0o700
# the names prune() may delete in <kind>/<2 hex digits>/: entries, and the temporary files
# tempfile.mkstemp names
BUCKET_NAME = re.compile(r"[0-9a-f]{2}")
ENTRY_NAME = re.compile(r"[0-9a-f]{64}")
TEMP_NAME = re.compile(re.escape(TEMP_PREFIX) + r"[a-z0-9_]+")
# an entry is never opened through a symbolic link, nor waits on a FIFO put in its place
READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)

# tokens and keys a file or an LLM reply might contain; such values never reach the disk
GITHUB_TOKEN_FORMAT = r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}"
OPENAI_ANTHROPIC_KEY_FORMAT = r"sk-[A-Za-z0-9_-]{20,}"
AWS_KEY_ID_FORMAT = r"(?:AKIA|ASIA)[0-9A-Z]{16}"   # long-term and temporary (STS) key IDs
SLACK_TOKEN_FORMAT = r"xox[a-z]-[0-9A-Za-z-]{10,}"
STRIPE_LIVE_KEY_FORMAT = r"[rs]k_live_[0-9A-Za-z]{20,}"
GOOGLE_API_KEY_FORMAT = r"AIza[0-9A-Za-z_-]{35}"
GITLAB_TOKEN_FORMAT = r"glpat-[0-9A-Za-z_-]{20,}"
HUGGING_FACE_TOKEN_FORMAT = r"hf_[A-Za-z0-9]{34}"
# the first line of a PEM or OpenSSH private key ("BEGIN RSA PRIVATE KEY" between dashes), or of
# a PGP one ("BEGIN PGP PRIVATE KEY BLOCK")
PRIVATE_KEY_HEADER = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
PUTTY_KEY_HEADER = r"PuTTY-User-Key-File-[0-9]+:"
CREDENTIAL = re.compile("|".join((
    GITHUB_TOKEN_FORMAT, OPENAI_ANTHROPIC_KEY_FORMAT, AWS_KEY_ID_FORMAT, SLACK_TOKEN_FORMAT,
    STRIPE_LIVE_KEY_FORMAT, GOOGLE_API_KEY_FORMAT, GITLAB_TOKEN_FORMAT, HUGGING_FACE_TOKEN_FORMAT,
    PRIVATE_KEY_HEADER, PUTTY_KEY_HEADER,
)))


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


def current_uid() -> int | None:
    # None where files have no POSIX owner and mode (Windows): only links are checked there
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def directory_problem(path: Path, uid: int | None) -> str | None:
    """Why the cache mustn't use the directory at `path`, or None if it is a real directory that
    belongs to the user. Raises OSError if there's nothing at `path`, or not a directory."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return "it is a symbolic link"
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"Not a directory: {str(path)!r}")
    if uid is not None and info.st_uid != uid:
        return "it belongs to another user"
    return None


def others_can_use(path: Path, uid: int | None) -> bool:
    # where files have no POSIX owner and mode (Windows), nothing is made private
    return uid is not None and bool(os.lstat(path).st_mode & 0o077)


def make_private(path: Path) -> str | None:
    """Makes the directory at `path` private to the user: why that failed, or None."""
    with contextlib.suppress(OSError):
        os.chmod(path, PRIVATE_MODE)  # pr-analyzer: ignore - makes the folder private (0700)
    if os.lstat(path).st_mode & 0o077:
        return "other users can open it, and it can't be made private"
    return None


def warn_unsafe(path: Path, problem: str, fix: str) -> None:
    log.warning("cache_dir_unsafe", path=str(path), problem=problem,
                hint=f"caching off for this run: {fix}")


def own_file(info: os.stat_result, uid: int | None) -> bool:
    # the cache never gives a file a second hard link: a file with one may be any other file
    # (another entry, say) linked into an entry's place
    return (
        stat.S_ISREG(info.st_mode) and info.st_nlink <= 1
        and (uid is None or info.st_uid == uid)
    )


def subdirectories(path: Path) -> list[Path]:
    """The directories in `path`, leaving out symbolic links to one; none if it can't be read."""
    try:
        with os.scandir(path) as items:
            return [Path(item.path) for item in items if item.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def regular_files(path: Path) -> list[tuple[Path, os.stat_result]]:
    """The regular files in `path` with their lstat, leaving out symbolic links; none if it
    can't be read."""
    files = []
    with contextlib.suppress(OSError), os.scandir(path) as items:
        for item in items:
            with contextlib.suppress(OSError):
                info = item.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    files.append((Path(item.path), info))
    return files


class DiskCache:
    """Safe to share between threads: every entry is its own file, and only the counters and
    the result of the directory check are shared state."""

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
        self._checked = False   # <dir> and <dir>/v1 passed their check
        self._unsafe = False    # a directory of the cache didn't: the cache is off for the run

    @classmethod
    def disabled(cls) -> "DiskCache":
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.root is not None and not self._unsafe

    def get(self, kind: str, *parts: str) -> str | None:
        if not self.enabled:
            return None
        path = self._path(kind, parts)
        usable = self._usable(create=False) and self._entry_dirs_usable(path, create=False)
        value = self._read(path) if usable else None
        with self._lock:
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
        return value

    def put(self, kind: str, *parts: str, value: str) -> None:
        if not self.enabled or not self.writable or not self.storable(value):
            return
        path = self._path(kind, parts)
        data = zlib.compress(json.dumps({"value": value}).encode("ascii"))
        try:
            if not (self._usable(create=True) and self._entry_dirs_usable(path, create=True)):
                return
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
        if not self.enabled:
            return
        log.info("cache_used", hits=self.hits, misses=self.misses, writes=self.writes)
        if self.writes and self._prune_due():
            self.prune()

    def prune(self) -> None:
        """Drop expired entries, then the least recently used until the cache fits its size."""
        if not self.enabled or not self._usable(create=False):
            return
        assert self.root is not None
        now = self.clock()
        entries = []
        for path, info in self._entries():
            age = now - info.st_mtime
            if age > self.max_age_seconds:
                discard(path)
            elif not path.name.startswith(TEMP_PREFIX):
                entries.append((info.st_mtime, info.st_size, path))
            elif age > PRUNE_INTERVAL_SECONDS:
                discard(path)   # left behind by a writer that crashed

        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            discard(path)
            total -= size
        stamp = self.root / PRUNE_STAMP
        with contextlib.suppress(OSError):
            # made anew, not touched: a symbolic link put in its place is removed, not followed
            discard(stamp)
            os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            os.utime(stamp, (now, now))

    def _path(self, kind: str, parts: tuple[str, ...]) -> Path:
        assert self.root is not None
        key = cache_key(kind, parts)
        return self.root / kind / key[:2] / key

    def _read(self, path: Path) -> str | None:
        now = self.clock()
        try:
            with open(os.open(path, READ_FLAGS), "rb") as handle:
                info = os.fstat(handle.fileno())
                # Windows has no O_NOFOLLOW: there, opening a symbolic link opens its target
                if stat.S_ISLNK(os.lstat(path).st_mode) or not own_file(info, current_uid()):
                    return None   # nobody else's file is ever served as an entry
                expired = now - info.st_mtime > self.max_age_seconds
                data = b"" if expired else handle.read()
        except OSError:
            return None   # not cached yet, unreadable, or a symbolic link
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

    def _usable(self, create: bool) -> bool:
        """Whether <dir> and <dir>/v1 are there and safe to use. They're checked until they pass,
        after being created if `create`, and one that isn't safe turns the cache off for the run.
        Raises OSError if `create` and they can't be made."""
        if self._checked:
            return True
        with self._lock:
            if not (self._checked or self._unsafe):
                try:
                    self._check_directories(create)
                except OSError:
                    if create:
                        raise
                    # not made yet, so there's nothing to read
            return self._checked

    def _check_directories(self, create: bool) -> None:
        """Checks <dir> and <dir>/v1, making either private if others can use it too. If one
        was, whatever is in <dir>/v1 may have been planted, so the cache's own files there are
        deleted before any is used."""
        assert self.root is not None
        uid = current_uid()
        made_private = False
        for path in (self.root.parent, self.root):
            if create:
                # private: the cache holds file contents, which may come from private repositories
                path.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
            problem = directory_problem(path, uid)
            if problem is None and others_can_use(path, uid):
                made_private = True
                problem = make_private(path)
            if problem is not None:
                self._unsafe = True
                warn_unsafe(path, problem, fix="use a CACHE_DIR of your own")
                return
        if made_private:
            self._clear()
        self._checked = True

    def _entry_dirs_usable(self, path: Path, create: bool) -> bool:
        """Whether <kind> and <kind>/<2 hex digits>, which hold the entry at `path`, are real
        directories of the user's, after being made if `create`. Through a symbolic link, or in
        someone else's directory, others could read entries or swap them, so either turns the
        cache off for the run. Raises OSError if `create` and they can't be made."""
        uid = current_uid()
        for directory in (path.parent.parent, path.parent):
            try:
                if create:
                    with contextlib.suppress(FileExistsError):
                        directory.mkdir()
                problem = directory_problem(directory, uid)
            except OSError:
                if create:
                    raise
                return False   # not made yet, so there's nothing to read
            if problem is not None:
                with self._lock:
                    first, self._unsafe = not self._unsafe, True
                if first:
                    warn_unsafe(directory, problem, fix="delete it, or use another CACHE_DIR")
                return False
        return True

    def _clear(self) -> None:
        """Deletes the cache's own files: every entry and temporary file that prune() may delete,
        whatever its age, and the prune stamp. A warning says how many went, if any did."""
        assert self.root is not None
        paths = [path for path, _ in self._entries()] + [self.root / PRUNE_STAMP]
        deleted = sum(discard(path) for path in paths)
        if deleted:
            log.warning("cache_cleared", path=str(self.root), files=deleted,
                        reason="others could use the cache folder until it was made private")

    def _prune_due(self) -> bool:
        assert self.root is not None
        try:
            info = os.lstat(self.root / PRUNE_STAMP)
        except OSError:
            return True
        # a stamp that isn't the cache's own file, such as a planted link, can't put pruning off
        if not own_file(info, current_uid()):
            return True
        return self.clock() - info.st_mtime > PRUNE_INTERVAL_SECONDS

    def _entries(self) -> Iterator[tuple[Path, os.stat_result]]:
        """The entry and temporary files in <kind>/<2 hex digits>/, with their lstat: the only
        files prune() may delete. No symbolic link is followed."""
        assert self.root is not None
        for kind in subdirectories(self.root):
            for bucket in subdirectories(kind):
                if not BUCKET_NAME.fullmatch(bucket.name):
                    continue
                for path, info in regular_files(bucket):
                    name = path.name
                    if TEMP_NAME.fullmatch(name) or (
                        ENTRY_NAME.fullmatch(name) and name.startswith(bucket.name)
                    ):
                        yield path, info


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


def discard(path: Path) -> bool:
    """Deletes the file at `path` (a symbolic link itself, not its target): whether it went."""
    try:
        path.unlink()
    except OSError:
        return False
    return True


def open_cache(enabled: bool = True) -> DiskCache:
    """The cache the settings describe, or a disabled one (CACHE_ENABLED=false or --no-cache)."""
    if not (enabled and settings.cache_enabled):
        return DiskCache.disabled()
    root = Path(settings.cache_dir).expanduser() if settings.cache_dir else default_cache_dir()
    return DiskCache(
        root,
        max_bytes=settings.cache_max_size_mb * MEGABYTE,
        max_age_seconds=settings.cache_max_age_days * DAY_SECONDS,
        secrets=(
            settings.github_token,
            settings.openai_api_key,
            settings.anthropic_api_key,
            settings.nvidia_api_key,
        ),
    )
