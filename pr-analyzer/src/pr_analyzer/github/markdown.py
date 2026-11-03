"""Neutralise untrusted text before the bot posts it on GitHub.

Much of what the bot posts is shaped by the PR: titles and descriptions the LLM wrote after
reading it, Semgrep messages that quote its code, and file names it chose. Posted as-is, that text
could ping people, hide the rest of the review inside an HTML comment, fake the bot's hidden
review tag, or show a convincing link. A zero-width space after the character that starts each
of these keeps the text looking the same while GitHub no longer recognises it:

    @                   mentions (@user, @org/team) and e-mail autolinks
    <                   raw HTML, including <!-- comments --> and <https://...> autolinks
    ]                   links and images, [text](url) and ![alt](src), and reference
                        definitions [x]: url
    :// www.            bare-URL autolinks
    # or GH- + digit    issue and PR references (#12, GH-12), which would add a cross-link to
                        the referenced issue
    & + # or name;      character references such as &#x40; and &commat;, which GitHub would
                        decode into the characters above

Single-line contexts (list items, table cells) also lose their newlines, so the text can't add
headings or rows of its own.
"""
import re

ZERO_WIDTH_SPACE = "\u200b"

# where a zero-width space goes, unless one is already there. GitHub decodes backslash escapes
# too, so GH\-12, http:\/\/ and www\. count as well.
_BREAK = re.compile(
    r"(?:(?<=[@<\]])"
    r"|(?<=:)(?=\\?/\\?/)"
    r"|(?<=www)(?=\\?\.)"
    r"|(?<=#)(?=\d)"
    r"|(?:(?<=gh-)|(?<=gh\\-))(?=\d)"
    r"|(?<=&)(?=#|[a-z][a-z0-9]*;)"
    r")(?!\u200b)",
    re.IGNORECASE,
)


def neutralise(text: str) -> str:
    """Untrusted text made safe to post as GitHub Markdown, still reading the same."""
    return _BREAK.sub(ZERO_WIDTH_SPACE, str(text))


def neutralise_line(text: str) -> str:
    """neutralise() for text that must stay on one line."""
    return neutralise(" ".join(str(text).split()))


def code_span(text: str) -> str:
    """Untrusted text as inline code. The fence is longer than any run of backticks inside, so
    a file name like a`b can't close the span early."""
    text = " ".join(str(text).split())
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"
