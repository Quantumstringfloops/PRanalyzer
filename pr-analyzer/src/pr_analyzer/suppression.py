"""Inline suppression of findings.

Every finding on a line whose text contains `pr-analyzer: ignore` (usually in a comment) is
dropped, whichever check reported it. The pipeline names the ignored lines in a warning, so a
reviewer can see what a PR chose to silence:

    subprocess.run(cmd, shell=True)  # pr-analyzer: ignore - cmd is a fixed string
"""
import re
from collections.abc import Iterable

from pr_analyzer.diff import iter_new_lines
from pr_analyzer.models import FileChange, Suggestion

IGNORE_MARKER = re.compile(r"\bpr-analyzer:\s*ignore\b")
# ignored lines named in the warning; any beyond this are only counted
MAX_LISTED_IGNORED = 10


def marked_lines(file: FileChange) -> set[int]:
    """Line numbers in the new version of the file that carry the marker."""
    numbered: Iterable[tuple[int, str]]
    if file.raw_content is not None:
        # split on "\n" only, so the numbering matches the diff's
        numbered = enumerate(file.raw_content.split("\n"), start=1)
    else:
        # the full content isn't always available; the patch still holds every added line
        numbered = ((line_no, text) for line_no, text, _ in iter_new_lines(file.patch or ""))
    return {line_no for line_no, text in numbered if IGNORE_MARKER.search(text)}


def split_ignored(
    suggestions: list[Suggestion], files: list[FileChange]
) -> tuple[list[Suggestion], list[Suggestion]]:
    """Return (kept, ignored)."""
    marked = {f.filename: marked_lines(f) for f in files}
    kept: list[Suggestion] = []
    ignored: list[Suggestion] = []
    for s in suggestions:
        if s.line is not None and s.line in marked.get(s.filename, set()):
            ignored.append(s)
        else:
            kept.append(s)
    return kept, ignored


def ignored_warning(ignored: list[Suggestion]) -> str:
    locations = sorted({(s.filename, s.line or 0) for s in ignored})
    listed = ", ".join(f"{name}:{line}" for name, line in locations[:MAX_LISTED_IGNORED])
    if len(locations) > MAX_LISTED_IGNORED:
        listed += f" and {len(locations) - MAX_LISTED_IGNORED} more"
    return f"Ignored {len(ignored)} finding(s) on lines marked 'pr-analyzer: ignore': {listed}"
