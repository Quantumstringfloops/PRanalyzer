"""Benchmark: analysing a PR against a fake GitHub API that answers every request after a delay.

    cd pr-analyzer
    PYTHONPATH=src python benchmarks/fetch_benchmark.py --files 50 --latency 0.1

A local HTTP server plays the GitHub REST API for one PR (repository, pull request, file list,
file contents) and Ollama's /api/chat, waiting --latency seconds before each GitHub answer and
--llm-latency before each LLM answer. Nothing leaves the machine and no token is used. It times:

- GithubClient.fetch_pr with one worker (sequential, as before) and with --workers;
- the same with a cold and a warm content cache, and after a push that changes one file;
- LLMEngine.analyze on the same PR, with a cold and a warm LLM cache.

Point PYTHONPATH at an older checkout's src to time that version; what it lacks is skipped.
"""
import argparse
import base64
import functools
import hashlib
import inspect
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import github

from pr_analyzer.config import settings
from pr_analyzer.github import client as client_module
from pr_analyzer.github.client import GithubClient
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.logging_config import setup_logging

try:   # added with the cache; missing in older versions
    from pr_analyzer.cache import DiskCache
except ImportError:
    DiskCache = None   # type: ignore[assignment,misc]

REPO = "bench/repo"
PR_NUMBER = 7
URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
DEFAULT_FILES = 50
DEFAULT_LATENCY = 0.1
DEFAULT_LLM_LATENCY = 1.0
DEFAULT_WORKERS = 8
DEFAULT_DIFF_LINES = 3000
LINES_PER_FILE = 40
PAGE_SIZE_DEFAULT = 30   # GitHub's default per_page
LOCALHOST = "127.0.0.1"
SHA_LENGTH = 40
MS_PER_SECOND = 1000
HEADER = "{:<52}{:>9}{:>10}"
ROW = "{:<52}{:>9.2f}{:>10}"
EMPTY_REVIEW = {"message": {"role": "assistant", "content": '{"suggestions": []}'}}


def show(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def blob_sha(text: str) -> str:
    # git's object id for the file's content, which GitHub reports as the file's "sha"
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def make_file(index: int, lines: list[str]) -> dict:
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)
    content = "\n".join(lines) + "\n"
    return {
        "filename": f"pkg/module_{index:03d}.py", "status": "added",
        "additions": len(lines), "deletions": 0, "changes": len(lines),
        "patch": patch, "sha": blob_sha(content), "content": content,
    }


def make_files(count: int) -> list[dict]:
    return [
        make_file(i, [f"def handler_{i}_{n}(value: int) -> int:" if n % 4 == 0
                      else "    return value" for n in range(LINES_PER_FILE)])
        for i in range(count)
    ]


class FakeGitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, files: list[dict], latency: float, llm_latency: float) -> None:
        super().__init__((LOCALHOST, 0), Handler)
        self.files = files
        self.head_sha = "1" * SHA_LENGTH
        self.latency = latency
        self.llm_latency = llm_latency
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{LOCALHOST}:{self.server_address[1]}"

    def count(self, kind: str) -> None:
        with self.lock:
            self.counts[kind] += 1

    def push(self) -> None:
        """A new commit on the PR that changes its first file only."""
        first = self.files[0]
        lines = first["content"].splitlines() + ["# edited"]
        self.files[0] = make_file(0, lines)
        self.head_sha = "2" * SHA_LENGTH


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FakeGitHub

    def log_message(self, format: str, *args: object) -> None:
        pass   # keep the benchmark output readable

    def do_GET(self) -> None:
        time.sleep(self.server.latency)
        parts = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        route = parts.path.removeprefix(f"/repos/{REPO}")
        if route == "":
            self.reply("repo", self.repo())
        elif route == f"/pulls/{PR_NUMBER}":
            self.reply("pull", self.pull())
        elif route == f"/pulls/{PR_NUMBER}/files":
            self.reply_page(query)
        elif route.startswith("/contents/"):
            self.reply_content(unquote(route.removeprefix("/contents/")), query.get("ref"))
        else:
            self.reply("other", {"message": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(self.server.llm_latency)
        self.reply("llm", EMPTY_REVIEW)

    def repo(self) -> dict:
        base = self.server.base_url
        return {"id": 1, "name": "repo", "full_name": REPO, "url": f"{base}/repos/{REPO}",
                "owner": {"login": "bench"}}

    def pull(self) -> dict:
        base = self.server.base_url
        return {
            "number": PR_NUMBER, "title": "Benchmark PR", "user": {"login": "bench"},
            "base": {"ref": "main", "sha": "0" * SHA_LENGTH},
            "head": {"ref": "feature", "sha": self.server.head_sha},
            "additions": sum(f["additions"] for f in self.server.files), "deletions": 0,
            "draft": False, "url": f"{base}/repos/{REPO}/pulls/{PR_NUMBER}",
        }

    def reply_page(self, query: dict) -> None:
        per_page = int(query.get("per_page", PAGE_SIZE_DEFAULT))
        page = int(query.get("page", 1))
        files = self.server.files
        listed = [{k: v for k, v in f.items() if k != "content"}
                  for f in files[(page - 1) * per_page:page * per_page]]
        headers = {}
        if page * per_page < len(files):
            url = (f"{self.server.base_url}/repos/{REPO}/pulls/{PR_NUMBER}/files"
                   f"?per_page={per_page}&page={page + 1}")
            headers["Link"] = f'<{url}>; rel="next"'
        self.reply("files", listed, headers=headers)

    def reply_content(self, path: str, ref: str | None) -> None:
        file = next((f for f in self.server.files if f["filename"] == path), None)
        if file is None or ref != self.server.head_sha:
            self.reply("contents", {"message": "Not Found"}, status=HTTPStatus.NOT_FOUND)
            return
        encoded = base64.b64encode(file["content"].encode()).decode()
        self.reply("contents", {
            "type": "file", "encoding": "base64", "content": encoded, "path": path,
            "name": path.rsplit("/", 1)[-1], "size": len(file["content"]), "sha": file["sha"],
            "url": f"{self.server.base_url}/repos/{REPO}/contents/{path}?ref={ref}",
        })

    def reply(
        self, kind: str, data: object, status: int = HTTPStatus.OK, headers: dict | None = None
    ) -> None:
        self.server.count(kind)
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def has_setting(name: str) -> bool:
    return name in type(settings).model_fields


def has_cache() -> bool:
    return DiskCache is not None and "cache" in inspect.signature(GithubClient).parameters


def fetch(server: FakeGitHub, workers: int, cache_dir: Path | None) -> tuple[float, int, object]:
    if has_setting("github_max_workers"):
        settings.github_max_workers = workers
    client = GithubClient(token="", **({"cache": DiskCache(cache_dir)} if cache_dir else {}))
    server.counts.clear()
    start = time.perf_counter()
    try:
        pr = client.fetch_pr(URL)
    finally:
        client.close()
    elapsed = time.perf_counter() - start
    assert [f.raw_content for f in pr.files] == [f["content"] for f in server.files]
    return elapsed, sum(server.counts.values()), pr


def review(server: FakeGitHub, pr: object, cache_dir: Path | None) -> tuple[float, int]:
    engine = LLMEngine(**({"cache": DiskCache(cache_dir)} if cache_dir else {}))
    server.counts.clear()
    start = time.perf_counter()
    engine.analyze(pr, [])   # type: ignore[arg-type]
    return time.perf_counter() - start, server.counts["llm"]


def run(args: argparse.Namespace) -> None:
    setup_logging("INFO" if args.verbose else "ERROR")
    server = FakeGitHub(make_files(args.files), args.latency, args.llm_latency)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # every Github client the code creates talks to the fake server
    client_module.Github = functools.partial(github.Github, base_url=server.base_url)
    settings.llm_provider, settings.llm_model = "ollama", "bench"
    settings.ollama_url = server.base_url
    settings.max_diff_lines = args.diff_lines

    show(f"{args.files} files, {args.latency * MS_PER_SECOND:.0f} ms per GitHub request, "
         f"{args.llm_latency * MS_PER_SECOND:.0f} ms per LLM request")
    show(HEADER.format("run", "seconds", "requests"))
    with tempfile.TemporaryDirectory(prefix="pr-analyzer-bench-") as tmp:
        cache = Path(tmp) if has_cache() else None
        seconds, requests, pr = fetch(server, 1, None)
        show(ROW.format("fetch_pr, 1 worker, no cache", seconds, requests))
        if has_setting("github_max_workers"):
            seconds, requests, pr = fetch(server, args.workers, None)
            show(ROW.format(f"fetch_pr, {args.workers} workers, no cache", seconds, requests))
        if cache is not None:
            for label in ("cache miss (cold)", "cache hit (warm)"):
                seconds, requests, pr = fetch(server, args.workers, cache)
                show(ROW.format(f"fetch_pr, {args.workers} workers, {label}", seconds, requests))

        seconds, calls = review(server, pr, cache)
        show(ROW.format("LLM review" + (", cache miss (cold)" if cache else ""), seconds, calls))
        if cache is not None:
            seconds, calls = review(server, pr, cache)
            show(ROW.format("LLM review, cache hit (warm)", seconds, calls))
            server.push()
            seconds, requests, pr = fetch(server, args.workers, cache)
            show(ROW.format("fetch_pr after a push that changes 1 file", seconds, requests))
    server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=DEFAULT_FILES)
    parser.add_argument("--latency", type=float, default=DEFAULT_LATENCY,
                        help="seconds before each GitHub answer")
    parser.add_argument("--llm-latency", type=float, default=DEFAULT_LLM_LATENCY,
                        help="seconds before each LLM answer")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--diff-lines", type=int, default=DEFAULT_DIFF_LINES,
                        help="MAX_DIFF_LINES: diff lines per LLM request")
    parser.add_argument("--verbose", action="store_true", help="show the progress logs")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
