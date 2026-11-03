"""Helpers for GitHub's per-file `patch` text.

GitHub's patch for one file is a list of unified-diff hunks with no `---`/`+++` file headers:

    @@ -10,7 +10,8 @@ def foo():     <- hunk header: the new file's hunk starts at line 10
     unchanged line                   <- context line (leading space)
    -removed line
    +added line
"""
import re
from collections.abc import Iterator

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def iter_new_lines(patch: str) -> Iterator[tuple[int, str, bool]]:
    """Yield (line number in the new file, text without the +/space prefix, is_added)
    for every line of the patch that exists in the new version of the file."""
    current: int | None = None
    for raw in patch.splitlines():
        hunk = HUNK_HEADER.match(raw)
        if hunk:
            current = int(hunk.group(1))
            continue
        if current is None or raw.startswith(("-", "\\")):
            # text before the first hunk, a removed line, or "\ No newline at end of file"
            continue
        text = raw[1:] if raw[:1] in ("+", " ") else raw
        yield current, text, raw.startswith("+")
        current += 1


def iter_added_lines(patch: str) -> Iterator[tuple[int, str]]:
    """Yield (line number in the new file, text) for each line the patch adds."""
    for line_no, text, is_added in iter_new_lines(patch):
        if is_added:
            yield line_no, text


def count_changes(patch: str) -> tuple[int, int]:
    """(lines added, lines removed) by the patch."""
    added = removed = 0
    in_hunk = False
    for raw in patch.splitlines():
        if HUNK_HEADER.match(raw):
            in_hunk = True
        elif in_hunk and raw.startswith("+"):
            added += 1
        elif in_hunk and raw.startswith("-"):
            removed += 1
    return added, removed


def commentable_lines(patch: str) -> set[int]:
    # GitHub accepts inline review comments only on lines that appear in the diff
    return {line_no for line_no, _, _ in iter_new_lines(patch)}


def annotate_patch(patch: str) -> str:
    """Prefix every line that exists in the new file with its line number, so an LLM reading
    the diff can cite exact lines. Removed lines keep their '-' and get no number."""
    out = []
    current: int | None = None
    for raw in patch.splitlines():
        hunk = HUNK_HEADER.match(raw)
        if hunk:
            current = int(hunk.group(1))
            out.append(raw)
        elif current is None or raw.startswith(("-", "\\")):
            out.append(f"{'':>6}{raw}")
        else:
            out.append(f"{current:>5} {raw}")
            current += 1
    return "\n".join(out)
