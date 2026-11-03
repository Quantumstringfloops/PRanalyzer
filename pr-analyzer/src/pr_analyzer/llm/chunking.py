"""Splitting a PR's diff into LLM requests of at most MAX_DIFF_LINES diff lines each.

A PR whose diff fits is one request, with exactly the prompt it always had. A bigger one becomes
several requests, each holding whole files in PR order. A file too big for one request is cut at
hunk boundaries, and a hunk too big for one request (a large new file is a single hunk) at line
boundaries, under a hunk header that keeps the line numbers right. A new 5,000-line file with
MAX_DIFF_LINES=3000 goes out in two parts:

    @@ -0,0 +1,5000 @@        lines 1-2998 (the file's name and this header use up two lines)
    @@ -0 +2999 @@            lines 2999-5000

Each request's prompt is built by build_prompt from a copy of the PR that holds only that
request's files, so every request is laid out, and guarded, like a single one.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, replace

from pr_analyzer.llm.prompts import build_prompt
from pr_analyzer.models import FileChange, PRSummary, Suggestion

# a hunk header with both start lines: @@ -<old>[,<count>] +<new>[,<count>] @@
HUNK_RANGES = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# rough size of a prompt in tokens: about four characters each
CHARS_PER_TOKEN = 4


@dataclass
class ReviewRequest:
    prompt: str
    filenames: list[str]   # the files whose diff, or part of it, the prompt holds


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def diff_lines(file: FileChange) -> int:
    # what build_prompt sends for a file: its "--- name ---" line and every line of its patch
    return 1 + len((file.patch or "").splitlines())


def hunks(lines: list[str]) -> list[list[str]]:
    """The patch's lines, one list per hunk (anything before the first header is a list too)."""
    groups: list[list[str]] = []
    for line in lines:
        if not groups or HUNK_RANGES.match(line):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def advance(old: int, new: int, lines: list[str]) -> tuple[int, int]:
    """Line numbers in the old and new file after `lines`, counted the way annotate_patch does."""
    for line in lines:
        if line.startswith("-"):
            old += 1
        elif line.startswith("+"):
            new += 1
        elif not line.startswith("\\"):   # "\ No newline at end of file" is no line at all
            old += 1
            new += 1
    return old, new


def cut_hunk(hunk: list[str], max_lines: int) -> list[list[str]]:
    """Cut one hunk into parts of at most `max_lines` lines. Every part after the first starts
    with a header naming the lines it starts at, so its lines keep their numbers."""
    ranges = HUNK_RANGES.match(hunk[0])
    if ranges is None:   # text before the first hunk: it has no line numbers to keep
        return [hunk[i:i + max_lines] for i in range(0, len(hunk), max(max_lines, 1))]

    room = max(max_lines - 1, 1)   # lines that fit beside a part's header
    old, new = int(ranges.group(1)), int(ranges.group(2))
    parts = []
    for start in range(1, len(hunk), room):
        body = hunk[start:start + room]
        header = hunk[0] if start == 1 else f"@@ -{old} +{new} @@"
        parts.append([header, *body])
        old, new = advance(old, new, body)
    return parts or [hunk]


def split_patch(patch: str, max_lines: int) -> list[str]:
    """Cut a patch into pieces of at most `max_lines` lines, at hunk boundaries where it can."""
    pieces: list[list[str]] = []
    for hunk in hunks(patch.splitlines()):
        for part in cut_hunk(hunk, max_lines) if len(hunk) > max_lines else [hunk]:
            if pieces and len(pieces[-1]) + len(part) <= max_lines:
                pieces[-1].extend(part)
            else:
                pieces.append(list(part))
    return ["\n".join(piece) for piece in pieces]


def plan_chunks(files: list[FileChange], max_lines: int) -> list[list[FileChange]]:
    """Group the files that have a patch into chunks of at most `max_lines` diff lines, in order.
    A file too big for one chunk is split into copies that each hold one piece of its patch."""
    chunks: list[list[FileChange]] = []
    size = 0
    for file in files:
        if not file.patch:
            continue
        pieces = [file]
        if diff_lines(file) > max_lines:
            pieces = [replace(file, patch=p) for p in split_patch(file.patch, max_lines - 1)]
        for piece in pieces:
            lines = diff_lines(piece)
            if chunks and size + lines <= max_lines:
                chunks[-1].append(piece)
                size += lines
            else:
                chunks.append([piece])
                size = lines
    return chunks


def review_requests(
    pr: PRSummary, existing_findings: list[Suggestion], max_lines: int
) -> list[ReviewRequest]:
    chunks = plan_chunks(pr.files, max_lines)
    if len(chunks) <= 1:
        # a diff that fits is sent as it always was
        names = [f.filename for f in pr.files if f.patch]
        return [ReviewRequest(build_prompt(pr, existing_findings, max_lines), names)]

    split = {f.filename for f in pr.files if f.patch and diff_lines(f) > max_lines}
    requests = []
    for number, chunk in enumerate(chunks, start=1):
        names = list(dict.fromkeys(f.filename for f in chunk))
        # the findings already reported for this chunk's files, so the model doesn't repeat them
        findings = [s for s in existing_findings if s.filename in names]
        prompt = build_prompt(replace(pr, files=chunk), findings, max_lines)
        partly = " (some of them only in part)" if split.intersection(names) else ""
        note = (
            f"This is part {number} of {len(chunks)} of the review of this pull request: the diff "
            f"above covers {len(names)} of its {len(pr.files)} changed files{partly}. The other "
            f"parts are reviewed in separate requests."
        )
        requests.append(ReviewRequest(f"{prompt}\n\n{note}", names))
    return requests


def split_files(requests: list[ReviewRequest]) -> set[str]:
    """The files reviewed in parts: those whose diff went out in more than one request."""
    counts = Counter(name for request in requests for name in request.filenames)
    return {name for name, count in counts.items() if count > 1}
