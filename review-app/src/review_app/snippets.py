"""The few diff lines behind a finding, kept with it so the dashboard can show what caused it.

They're taken from the pull request's patch when the analysis is recorded, because the
dashboard holds no GitHub credentials and never calls GitHub. The window is a few lines around
the finding's line, and stays inside its diff hunk. Anything pr-analyzer's secret rules would
flag, or any long token-like run, is masked before it's stored, so a "hardcoded password"
finding doesn't keep the password."""
import re
from typing import Any

from pr_analyzer.security.scanner import SECRET_PATTERNS

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# a long unbroken run of key-like characters: the body of a private key, a token, a hash
TOKEN_LIKE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
RADIUS = 3
MAX_LINE = 180
MASK = "••••••"


def diff_rows(patch: str) -> list[tuple[str, int | None, str]]:
    """(kind, line in the new file, text) for each line of a unified diff. Kind is "+" (added),
    "-" (removed, so no new line number), " " (unchanged), or "@" (a hunk header)."""
    rows: list[tuple[str, int | None, str]] = []
    new: int | None = None
    for raw in patch.splitlines():
        header = HUNK.match(raw)
        if header:
            new = int(header.group(1))
            rows.append(("@", None, raw))
            continue
        if new is None or raw.startswith("\\"):    # "\ No newline at end of file"
            continue
        kind, text = raw[:1] or " ", raw[1:]
        if kind == "-":
            rows.append(("-", None, text))
        else:
            rows.append(("+" if kind == "+" else " ", new, text))
            new += 1
    return rows


def snippet(patch: str | None, line: int | None, radius: int = RADIUS) -> list[dict[str, Any]]:
    """The diff rows within `radius` of the finding's line (a line of the new file), with the
    finding's own row marked "hit". Empty when there's no patch, or the line isn't in it."""
    if not patch or not line:
        return []
    rows = diff_rows(patch)
    hit = next((i for i, (kind, number, _) in enumerate(rows)
                if number == line and kind != "@"), None)
    if hit is None:
        return []
    start = hit
    while start > 0 and hit - start < radius and rows[start - 1][0] != "@":
        start -= 1
    end = hit
    while end < len(rows) - 1 and end - hit < radius and rows[end + 1][0] != "@":
        end += 1
    return [{"kind": kind, "line": number, "text": redact(text)[:MAX_LINE], "hit": i == hit}
            for i, (kind, number, text) in enumerate(rows[start:end + 1], start)]


def redact(text: str) -> str:
    """The line with what pr-analyzer's secret rules match, and long token-like runs, masked."""
    for pattern in [*SECRET_PATTERNS, {"regex": TOKEN_LIKE}]:
        group = pattern.get("value_group")
        spans = [match.span(group) if group else match.span()
                 for match in pattern["regex"].finditer(text)]
        for start, end in reversed(spans):
            if end > start:
                text = text[:start] + MASK + text[end:]
    return text
