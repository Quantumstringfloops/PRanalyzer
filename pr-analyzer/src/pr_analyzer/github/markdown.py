"""Neutralise untrusted text before the bot posts it on GitHub.

Much of what the bot posts is shaped by the PR: titles and descriptions the LLM wrote after
reading it, Semgrep messages that quote its code, and file names it chose. Posted as-is, that text
could ping people, hide the rest of the review inside an HTML comment, fake the bot's hidden
review tag, or show a convincing link. A zero-width space after the character that starts each
of these keeps the text looking the same while GitHub no longer recognises it:

    @                   mentions (@user, @org/team) and e-mail autolinks
    < + letter / ! ?    raw HTML, including <!-- comments --> and <https://...> autolinks
    ] + ( [ :           links and images, [text](url), [text][ref] and ![alt](src), and
                        reference definitions [x]: url
    :// www.            bare-URL autolinks
    # or GH- + digit    issue and PR references (#12, GH-12), which would add a cross-link to
                        the referenced issue
    & + # or name;      character references such as &#x40; and &commat;, which GitHub would
                        decode into the characters above

Nothing else changes. A "<" or "]" that can't start any of these is left as it is, and so is
code: GitHub shows inline code spans and fenced code blocks exactly as written, and an invisible
character in them would break copying the code or committing a suggested change. Only "]:" in
inline code still gets one, since a reference definition is read before code spans are. Whatever
GitHub might not show as code is neutralised like the rest: blocks it draws instead (maths,
diagrams), code that a "$" could turn into maths, and code whose place can't be told for sure.

Single-line contexts (list items, table cells) also lose their newlines, so the text can't add
headings or rows of its own.
"""
import re

ZERO_WIDTH_SPACE = "\u200b"

# where a zero-width space goes, unless one is already there. GitHub decodes backslash escapes
# too, so GH\-12, http:\/\/ and www\. count as well.
_BREAK = re.compile(
    r"(?:(?<=@)"
    r"|(?<=<)(?=[a-z/!?])"
    r"|(?<=\])(?=[(\[:])"
    r"|(?<=:)(?=\\?/\\?/)"
    r"|(?<=www)(?=\\?\.)"
    r"|(?<=#)(?=\d)"
    r"|(?:(?<=gh-)|(?<=gh\\-))(?=\d)"
    r"|(?<=&)(?=#|[a-z][a-z0-9]*;)"
    r")(?!\u200b)",
    re.IGNORECASE,
)
# A reference definition, [label]: url, is read before code spans are, so that a later [label]
# links to url. Inline code still gets a zero-width space between "]" and ":".
_LABEL_END = re.compile(r"(?<=\])(?=:)")

# CommonMark's line endings; split() keeps them, so the text goes back together unchanged
_LINE_ENDING = re.compile(r"(\r\n|\r|\n)")
_BLANK = re.compile(r"[ \t]*")
# a line that opens a fenced code block: three or more backticks with none in the info string
# after them, or three or more tildes
_FENCE_OPEN = re.compile(r"(`{3,})([^`]*)|(~{3,})(.*)", re.DOTALL)
_FENCE_CLOSE = re.compile(r" {0,3}(`+|~+)[ \t]*")
# a fence indented by one to three spaces, which may belong to a list item
_INDENTED_FENCE = re.compile(r" {1,3}(?:```|~~~)")
# what could be a fence anywhere in a line, as in a list item or a block quote
_FENCE_RUN = re.compile(r"`{3,}|~{3,}")
# GitHub draws these blocks (maths, diagrams, maps, models) instead of showing them as code, and
# the drawing could hold a link; so could maths written with "$" around code. Both are text here.
_DRAWN = {"math", "mermaid", "geojson", "topojson", "stl"}
# a plain language name in an info string, such as python, c++ or objective-c
_LANGUAGE = re.compile(r"[\w+#.-]+")
# what an inline parser stops at on its way to a code span: a backslash escape or backticks
_ESCAPE_OR_BACKTICKS = re.compile(r"\\[!-/:-@\[-`{-~]|`+")
# an e-mail autolink, <name@host>, whose name holds a backtick: the autolink comes first
_MAIL_WITH_BACKTICK = re.compile(r"<[\w.!#$%&'*+/=?^`{|}~-]*`[\w.!#$%&'*+/=?^`{|}~-]*@")
# GitHub's parser (cmark-gfm) never opens a code span with a longer run of backticks
_MAX_BACKTICKS = 1000


def neutralise(text: str) -> str:
    """Untrusted text made safe to post as GitHub Markdown, still reading the same. The text must
    start a block of its own at the top level of the comment, as a review comment's description
    does after a blank line; in a list item or a table cell, use neutralise_line()."""
    parts = _LINE_ENDING.split(str(text))
    lines = parts[::2]
    unsure = False   # whether an earlier line of the paragraph makes its code spans uncertain
    for i, block in enumerate(_fenced_blocks(lines)):
        if block == "code" or _BLANK.fullmatch(lines[i]):
            unsure = False   # a code block or a blank line ends the paragraph
            continue
        lines[i], unsettling = _neutralise_inline(lines[i], trust_code=block == "" and not unsure)
        # so does a drawn block, whose lines are all neutralised
        unsure = block == "" and (unsure or unsettling)
    parts[::2] = lines
    return "".join(parts)


def neutralise_line(text: str) -> str:
    """neutralise() for text that must stay on one line."""
    return _neutralise_inline(" ".join(str(text).split()))[0]


def code_span(text: str) -> str:
    """Untrusted text as inline code. The fence is longer than any run of backticks inside, so
    a file name like a`b can't close the span early."""
    text = " ".join(str(text).split())
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def comparable(text: str) -> str:
    """The text as the review compares it with comments already posted, whichever version
    posted them: without zero-width spaces, which versions of neutralise() put in different
    places, with every line ending as "\\n", and without spaces or tabs at the end of a line or
    whitespace around the text. Nothing else changes, so text that differs in anything more,
    such as code and its indentation, still differs."""
    lines = _LINE_ENDING.split(str(text).replace(ZERO_WIDTH_SPACE, ""))[::2]
    return "\n".join(line.rstrip(" \t") for line in lines).strip()


def _neutralise_inline(line: str, trust_code: bool = True) -> tuple[str, bool]:
    """One line of Markdown neutralised outside its code spans, and whether the later lines of
    its paragraph can no longer trust theirs: a run of backticks left open could pair with one of
    theirs, and after a "$" their code could be maths."""
    spans, left_open = _code_spans(line)
    # a run left open on an earlier line, or an e-mail autolink, would pair the backticks
    # differently, and a table splits its rows at every "|", inside code spans too: then none of
    # the line counts as code
    if (not trust_code or _MAIL_WITH_BACKTICK.search(line)
            or any("|" in line[start:end] for start, end in spans)):
        spans = []
    # code after a "$" could be maths, $`...`$ or inside $...$ or $$...$$, which GitHub draws
    dollar = line.find("$")
    if dollar != -1:
        spans = [(start, end) for start, end in spans if start < dollar]
    pieces, pos = [], 0
    for start, end in spans:
        code = _LABEL_END.sub(ZERO_WIDTH_SPACE, line[start:end])
        pieces += [_BREAK.sub(ZERO_WIDTH_SPACE, line[pos:start]), code]
        pos = end
    pieces.append(_BREAK.sub(ZERO_WIDTH_SPACE, line[pos:]))
    return "".join(pieces), left_open or dollar != -1


def _code_spans(line: str) -> tuple[list[tuple[int, int]], bool]:
    """The (start, end) of each inline code span in one line, found as CommonMark finds them, and
    whether a run of backticks found no partner on the line.

    A span opens at a run of backticks that isn't backslash-escaped and closes at the next run of
    exactly as many; a backslash inside a span is just a backslash. A run with no partner is
    plain text, and the search goes on after it."""
    spans: list[tuple[int, int]] = []
    left_open = False
    pos = 0
    while opening := _ESCAPE_OR_BACKTICKS.search(line, pos):
        pos = opening.end()
        if opening[0].startswith("\\") or len(opening[0]) > _MAX_BACKTICKS:
            continue
        closing = re.compile(rf"(?<!`){opening[0]}(?!`)").search(line, pos)
        if closing:
            spans.append((opening.start(), closing.end()))
            pos = closing.end()
        else:
            left_open = True
    return spans, left_open


def _fenced_blocks(lines: list[str]) -> list[str]:
    """For each line, the fenced block it belongs to, fences included: "code" for one GitHub
    shows as code, "drawn" for one it draws instead (see _DRAWN), "" for none.

    A fence at the very start of a line can't be inside a list item or a block quote, so its block
    ends only at its closing fence (or the end of the text). One indented by a few spaces may be in
    a list item, which a later line can end before the fence does, so from there on no fence is
    trusted. Once a drawn block may have opened where its end can't be told, every later line
    counts as drawn."""
    blocks: list[str] = []
    fence = kind = ""   # the open block's fence, such as ``` or ~~~~, and its kind
    trusted = True
    rest = ""   # what lines outside a trusted block count as
    for line in lines:
        if fence:
            blocks.append(kind)
            fence = "" if _closes(line, fence) else fence
            continue
        opening = _FENCE_OPEN.fullmatch(line) if trusted else None
        if opening:
            fence = opening[1] or opening[3]
            kind = "code" if _shown_as_code(opening[2] if opening[1] else opening[4]) else "drawn"
            blocks.append(kind)
            continue
        if any(not _shown_as_code(line[run.end():]) for run in _FENCE_RUN.finditer(line)):
            rest = "drawn"
        trusted = trusted and not _INDENTED_FENCE.match(line)
        blocks.append(rest)
    return blocks


def _closes(line: str, fence: str) -> bool:
    """Whether the line is a closing fence for a block opened with `fence`."""
    closing = _FENCE_CLOSE.fullmatch(line)
    return closing is not None and closing[1][0] == fence[0] and len(closing[1]) >= len(fence)


def _shown_as_code(info: str) -> bool:
    """Whether GitHub shows a fenced block with this info string as code. The first word names
    the language; one that isn't a plain name could still decode to the name of a drawn block.
    Zero-width spaces don't count, so that neutralising the text again can't change the answer."""
    words = info.replace(ZERO_WIDTH_SPACE, "").split()
    if not words:
        return True
    return _LANGUAGE.fullmatch(words[0]) is not None and words[0].lower() not in _DRAWN
