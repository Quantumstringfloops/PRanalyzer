"""Tell code, string literals and comments apart on one line of source.

The line rules look at one added line at a time. A rule about a call must not fire on text inside
a string or a comment, and the rule about to-do markers should fire only in comments, so a line
is split into two views of the same length:

    line:      x = run(y)  # run("1") is fine
    code:      x = run(y)
    comments:              # run("1") is fine

In the code view a string keeps its quotes and prefix but its contents become spaces
(`f"SELECT {id}"` -> `f"           "`), so rules about how a string is used, such as the
SQL-injection rule, still see it.

Python, JavaScript/TypeScript and C-family files are lexed; for any other language both views are
the whole line, which is how every rule matched before. Limits, since each line is read alone:

- a line inside a multi-line string (a docstring's middle lines, a template literal spanning
  lines) or inside a /* */ block is read as code, unless it starts like a comment (`*`, `#`, `//`);
- code inside f-string braces and `${...}` is treated as part of the string;
- JavaScript regex literals aren't recognised, so a quote inside one hides the rest of the line;
- in C-family files only double-quoted strings are recognised.
"""
import re
from dataclasses import dataclass

COMMENT_PREFIXES = ("#", "//", "/*", "*")

_STRING = r'"(?:\\.|[^"\\])*(?:"|$)'          # an unclosed string runs to the end of the line
_SINGLE_QUOTED = r"'(?:\\.|[^'\\])*(?:'|$)"
_BLOCK_COMMENT = r"/\*.*?(?:\*/|$)"

PYTHON = re.compile(
    r"(?P<comment>#.*)"
    r'|(?P<string>"""(?:\\.|[^\\])*?(?:"""|$)'
    r"|'''(?:\\.|[^\\])*?(?:'''|$)"
    rf"|{_STRING}|{_SINGLE_QUOTED})"
)
JAVASCRIPT = re.compile(
    rf"(?P<comment>//.*|{_BLOCK_COMMENT})"
    rf"|(?P<string>{_STRING}|{_SINGLE_QUOTED}|`(?:\\.|[^`\\])*(?:`|$))"
)
# single quotes are char literals or (in Rust) lifetimes, so only "..." counts as a string
C_FAMILY = re.compile(rf"(?P<comment>//.*|{_BLOCK_COMMENT})|(?P<string>{_STRING})")

LEXERS = {".py": PYTHON}
LEXERS.update(dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"), JAVASCRIPT))
LEXERS.update(dict.fromkeys(
    (".java", ".go", ".cs", ".kt", ".swift", ".scala", ".rs", ".php",
     ".c", ".h", ".cc", ".cpp", ".hpp"),
    C_FAMILY,
))


@dataclass(frozen=True)
class LineParts:
    code: str       # string contents and comments blanked out
    comments: str   # only the comments; everything else blanked out


def is_comment(text: str) -> bool:
    return text.lstrip().startswith(COMMENT_PREFIXES)


def split_line(text: str, extension: str) -> LineParts:
    lexer = LEXERS.get(extension)
    # The middle lines of a /* */ block start with "*", which a lexer can't tell from one line.
    # Without a lexer, any comment prefix marks a whole-line comment, as it always did.
    if text.lstrip().startswith("*") or (lexer is None and is_comment(text)):
        return LineParts(" " * len(text), text)
    if lexer is None:
        return LineParts(text, text)

    code, comments, end = [], [], 0
    for token in lexer.finditer(text):
        gap = text[end:token.start()]
        code.append(gap)
        comments.append(" " * len(gap))
        if token.lastgroup == "comment":
            code.append(" " * len(token.group()))
            comments.append(token.group())
        else:
            code.append(blank_string(token.group()))
            comments.append(" " * len(token.group()))
        end = token.end()
    code.append(text[end:])
    comments.append(" " * (len(text) - end))
    return LineParts("".join(code), "".join(comments))


def blank_string(literal: str) -> str:
    """Keep a string literal's quotes and blank everything between them."""
    quote = literal[:3] if literal[:3] in ('"""', "'''") else literal[0]
    closed = len(literal) >= 2 * len(quote) and literal.endswith(quote)
    inside = len(literal) - len(quote) * (2 if closed else 1)
    return quote + " " * inside + (quote if closed else "")
