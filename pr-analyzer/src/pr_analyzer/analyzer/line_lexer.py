"""Tell code, string literals and comments apart on one line of source.

The line rules look at one added line at a time. A rule about a call must not fire on text inside
a string or a comment, and the rule about to-do markers should fire only in comments, so a line
is split into two views of the same length:

    line:      x = run(y)  # run("1") is fine
    code:      x = run(y)
    comments:              # run("1") is fine

In the code view a string keeps its quotes and prefix but its contents become spaces
(`"SELECT " + id` -> `"       " + id`), so rules about how a string is used, such as the
SQL-injection rule, still see it. The replacement fields of an f-string or a template literal are
code, so they stay; only the text around them, and a format spec, is blanked
(`f"id {run(x):>8}"` -> `f"   {run(x)    }"`, `` `a ${run(b)}` `` -> `` `  ${run(b)}` ``).

Python, JavaScript/TypeScript and C-family files are lexed; for any other language both views are
the whole line, which is how every rule matched before. Limits, since each line is read alone:

- a line inside a multi-line string (a docstring's middle lines, a template literal spanning
  lines) or inside a /* */ block is read as code, unless it starts like a comment (`*`, `#`, `//`);
- JavaScript regex literals aren't recognised, so a quote inside one hides the rest of the line;
- in C-family files only double-quoted strings are recognised.
"""
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

COMMENT_PREFIXES = ("#", "//", "/*", "*")
# replacement fields nested deeper than this (never seen in real code) end the lexing of a line,
# so a crafted line can't exhaust the recursion limit
MAX_NESTING = 20

_STRING = r'"(?:\\(?:.|$)|[^"\\])*(?:"|$)'          # an unclosed string runs to the end of the line
_SINGLE_QUOTED = r"'(?:\\(?:.|$)|[^'\\])*(?:'|$)"
_TRIPLE_QUOTED = r'"""(?:\\(?:.|$)|[^\\])*?(?:"""|$)' r"|'''(?:\\(?:.|$)|[^\\])*?(?:'''|$)"
_BLOCK_COMMENT = r"/\*.*?(?:\*/|$)"
# inside a replacement field brackets are counted, so the field ends at its own closing brace
_BRACKETS = r"|(?P<open>[(\[{])|(?P<close>[)\]}])"
# a format spec is text up to the end of its field, apart from the fields nested in it
_FORMAT_SPEC = re.compile(r"[^{}]*")
_REST_OF_LINE = re.compile(r".*")


@dataclass(frozen=True)
class Syntax:
    """How a family of languages writes comments and strings."""
    tokens: re.Pattern[str]         # a comment, a string, or the start of an interpolated string
    field_tokens: re.Pattern[str]   # the same plus brackets, inside a replacement field
    # The text of an interpolated string up to a replacement field, by (quote, raw), and what
    # opens a field: Python's f-strings and t-strings and JavaScript's template literals have them.
    template_text: Mapping[tuple[str, bool], re.Pattern[str]] = field(default_factory=dict)
    field_opener: str = ""


def _fstring_text(quote: str, raw: bool) -> re.Pattern[str]:
    """An f-string's text up to a replacement field, its closing quote or the end of the line.
    `{{` and `}}` stand for a brace, a backslash never escapes `{`, and `\\N{...}` names a
    character unless the string is raw."""
    q = quote[0]
    named = "" if raw else r"\\N\{[^}]*\}?|"
    lone_quote = f"|{q}(?!{q}{q})" if len(quote) == 3 else ""
    return re.compile(
        "(?:" + named + r"\\(?:[^{]|(?=\{)|$)|\{\{|\}|[^\\{}" + q + "]" + lone_quote + ")*"
    )


_PYTHON_TOKENS = (
    r"(?P<comment>#.*)"
    # an f-string or a t-string: its replacement fields hold code, so its body is lexed apart
    r"|(?P<template>(?<!\w)(?P<prefix>[rRbBuU]?[fFtT][rRbBuU]?)(?P<quote>'''|\"\"\"|'|\"))"
    rf"|(?P<string>{_TRIPLE_QUOTED}|{_STRING}|{_SINGLE_QUOTED})"
)
PYTHON = Syntax(
    tokens=re.compile(_PYTHON_TOKENS),
    # a ":" outside brackets ends a field's expression and starts its format spec
    field_tokens=re.compile(_PYTHON_TOKENS + _BRACKETS + r"|(?P<colon>:)"),
    template_text={
        (quote, raw): _fstring_text(quote, raw)
        for quote in ('"', "'", '"""', "'''") for raw in (False, True)
    },
    field_opener="{",
)

_JAVASCRIPT_TOKENS = (
    rf"(?P<comment>//.*|{_BLOCK_COMMENT})"
    r"|(?P<template>(?P<quote>`))"
    rf"|(?P<string>{_STRING}|{_SINGLE_QUOTED})"
)
JAVASCRIPT = Syntax(
    tokens=re.compile(_JAVASCRIPT_TOKENS),
    field_tokens=re.compile(_JAVASCRIPT_TOKENS + _BRACKETS),
    template_text={("`", False): re.compile(r"(?:\\(?:.|$)|\$(?!\{)|[^\\$`])*")},
    field_opener="${",
)

# single quotes are char literals or (in Rust) lifetimes, so only "..." counts as a string
_C_TOKENS = re.compile(rf"(?P<comment>//.*|{_BLOCK_COMMENT})|(?P<string>{_STRING})")
C_FAMILY = Syntax(tokens=_C_TOKENS, field_tokens=_C_TOKENS)

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
    syntax = LEXERS.get(extension)
    # The middle lines of a /* */ block start with "*", which a lexer can't tell from one line.
    # Without a lexer, any comment prefix marks a whole-line comment, as it always did.
    if text.lstrip().startswith("*") or (syntax is None and is_comment(text)):
        return LineParts(" " * len(text), text)
    if syntax is None:
        return LineParts(text, text)
    return _Lexer(text, syntax).split()


def blank_string(literal: str) -> str:
    """Keep a string literal's quotes and blank everything between them."""
    quote = literal[:3] if literal[:3] in ('"""', "'''") else literal[0]
    closed = len(literal) >= 2 * len(quote) and literal.endswith(quote)
    inside = len(literal) - len(quote) * (2 if closed else 1)
    return quote + " " * inside + (quote if closed else "")


class _Lexer:
    """Builds the two views of one line from left to right."""

    def __init__(self, text: str, syntax: Syntax) -> None:
        self.text = text
        self.syntax = syntax
        self.code_view: list[str] = []
        self.comments_view: list[str] = []
        self.nesting = 0   # replacement fields open around the current position

    def split(self) -> LineParts:
        self.lex_code(0)
        return LineParts("".join(self.code_view), "".join(self.comments_view))

    def keep_code(self, chunk: str) -> None:
        self.code_view.append(chunk)
        self.comments_view.append(" " * len(chunk))

    def keep_comment(self, chunk: str) -> None:
        self.code_view.append(" " * len(chunk))
        self.comments_view.append(chunk)

    def skip_text(self, pattern: re.Pattern[str], pos: int) -> int:
        """Blank the string text `pattern` matches at `pos` in both views; return where it ends."""
        match = pattern.match(self.text, pos)
        end = match.end() if match else pos
        self.code_view.append(" " * (end - pos))
        self.comments_view.append(" " * (end - pos))
        return end

    def lex_code(self, pos: int, in_field: bool = False) -> int:
        """Lex code from `pos` to the end of the line or, in a replacement field, up to the `}`
        that closes the field or the `:` that starts its format spec. Returns where it stopped."""
        tokens = self.syntax.field_tokens if in_field else self.syntax.tokens
        depth = 0   # brackets opened inside the field
        while (token := tokens.search(self.text, pos)) is not None:
            self.keep_code(self.text[pos:token.start()])
            pos = token.end()
            kind, lexeme = token.lastgroup, token.group()
            if kind == "comment":
                self.keep_comment(lexeme)
            elif kind == "string":
                self.keep_code(blank_string(lexeme))
            elif kind == "template":
                pos = self.lex_template(token)
            elif kind == "open":
                depth += 1
                self.keep_code(lexeme)
            elif depth == 0 and lexeme in ("}", ":"):
                return token.start()
            else:   # a bracket closing inside the field, or a colon inside brackets
                depth = max(depth - 1, 0) if kind == "close" else depth
                self.keep_code(lexeme)
        self.keep_code(self.text[pos:])
        return len(self.text)

    def lex_template(self, opening: re.Match[str]) -> int:
        """Lex an f-string or template literal from its opening: blank its text and keep the code
        in its replacement fields. Returns where the string ends."""
        self.keep_code(opening.group())   # the prefix and the quote
        quote = opening["quote"]
        raw = "r" in (opening.groupdict().get("prefix") or "").lower()
        literal = self.syntax.template_text[quote, raw]
        opener = self.syntax.field_opener
        pos = self.skip_text(literal, opening.end())
        while pos < len(self.text) and not self.text.startswith(quote, pos):
            self.keep_code(opener)
            pos = self.skip_text(literal, self.lex_field(pos + len(opener)))
        if pos < len(self.text):
            self.keep_code(quote)
            pos += len(quote)
        return pos

    def lex_field(self, pos: int) -> int:
        """Lex a replacement field from just after its opener: its code, then in Python its
        format spec, up to and including the closing brace. Returns where the field ends."""
        if self.nesting == MAX_NESTING:   # the rest of the line counts as text
            return self.skip_text(_REST_OF_LINE, pos)
        self.nesting += 1
        pos = self.lex_code(pos, in_field=True)
        if self.text.startswith(":", pos):
            pos = self.lex_format_spec(pos)
        self.nesting -= 1
        if self.text.startswith("}", pos):
            self.keep_code("}")
            pos += 1
        return pos

    def lex_format_spec(self, pos: int) -> int:
        """A format spec (`:>8`) is text, apart from the fields nested in it (`:{width}`).
        Returns where it ends: at the brace closing its field, or the end of the line."""
        pos = self.skip_text(_FORMAT_SPEC, pos)
        while self.text.startswith("{", pos):
            self.keep_code("{")
            pos = self.skip_text(_FORMAT_SPEC, self.lex_field(pos + 1))
        return pos
