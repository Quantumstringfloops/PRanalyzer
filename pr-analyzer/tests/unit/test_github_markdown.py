import random
import re

import pytest

from pr_analyzer.github.markdown import ZERO_WIDTH_SPACE as ZW
from pr_analyzer.github.markdown import code_span, neutralise, neutralise_line

# ── what gets neutralised ─────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("thanks @octocat", f"thanks @{ZW}octocat"),                         # mentions
    ("ping @org/security-team", f"ping @{ZW}org/security-team"),
    ("mail admin@example.com", f"mail admin@{ZW}example.com"),           # e-mail autolinks
    ("<!-- pr-analyzer -->", f"<{ZW}!-- pr-analyzer -->"),               # the bot's hidden tag
    ("<img src=x onerror=alert(1)>", f"<{ZW}img src=x onerror=alert(1)>"),   # raw HTML
    ("<details><summary>x</summary>", f"<{ZW}details><{ZW}summary>x<{ZW}/summary>"),
    ("[fix](https://evil.example)", f"[fix]{ZW}(https:{ZW}//evil.example)"),   # links
    ("![logo](http://evil/a.png)", f"![logo]{ZW}(http:{ZW}//evil/a.png)"),     # images
    ("[fix][1]", f"[fix]{ZW}[1]"),                                             # reference links
    ("[docs]: https://evil.example", f"[docs]{ZW}: https:{ZW}//evil.example"), # and definitions
    ("see <https://evil.example>", f"see <{ZW}https:{ZW}//evil.example>"),     # autolinks
    ("go to www.evil.example now", f"go to www{ZW}.evil.example now"),
    ("fixes #12 and org/repo#3", f"fixes #{ZW}12 and org/repo#{ZW}3"),         # cross-references
    ("dup of GH-1, see gh-22", f"dup of GH-{ZW}1, see gh-{ZW}22"),
    # the rendered text has its backslash escapes decoded, so escaping hides nothing
    ("fixes GH\\-3", f"fixes GH\\-{ZW}3"),
    ("http:\\/\\/evil.example", f"http:{ZW}\\/\\/evil.example"),
    ("www\\.evil.example", f"www{ZW}\\.evil.example"),
])
def test_neutralise(text, expected):
    assert neutralise(text) == expected

def test_plain_text_is_unchanged():
    text = "eval() usage detected: avoid eval on user input (rule: python.eval)"   # pr-analyzer: ignore
    assert neutralise(text) == text

@pytest.mark.parametrize("text", [
    "pip install 'pranalyzer[security]'",   # the bot's own install hint, which pip then refused
    "rows[0] and data['users']",
    "a < b, a <= b, 1<2",
    "[optional] arguments",
    "a && b, ?x=1&y=2, AT&T",
    "C# and #define",
])
def test_text_that_can_start_nothing_is_unchanged(text):
    # only a "<" or "]" that could open HTML, an autolink or a link gets a zero-width space
    assert neutralise(text) == text and neutralise_line(text) == text

def test_text_still_reads_the_same():
    text = "Ask @alice about <b>[this](http://x)</b> in #4, GH-5 or www.example.org &#x40;bob"
    assert neutralise(text).replace(ZW, "") == text

@pytest.mark.parametrize("text", [
    "@a <b> [c](d) e://f www.g #1 GH-2 &#64;h `@i`\n```\n@j\n```\nk@l",
    "`]:` $`@m`$\n~~~math\n@n\n~~~\nx ~~~ GH-1\n<`@o>",
    "> ```gh-1\n\n`@a`",   # once gh-1 is broken, it must still read as a language name
])
def test_neutralising_twice_changes_nothing_more(text):
    # the review's "already posted" check compares comment bodies byte for byte
    once = neutralise(text)
    assert neutralise(once) == once

def test_one_line_text_loses_its_newlines():
    # a newline in a list item would let the text start its own heading or table
    assert neutralise_line("Risky\n\n## ✅ PASSED\n@x") == f"Risky ## ✅ PASSED @{ZW}x"

# ── character references ──────────────────────────────────────────────────────
# GitHub decodes these before it looks for mentions and references: &#x40;alice pinged alice
@pytest.mark.parametrize("text, expected", [
    ("ask &#x40;alice", f"ask &{ZW}#x40;alice"),
    ("ask &#X40;alice", f"ask &{ZW}#X40;alice"),
    ("ask &#64;alice", f"ask &{ZW}#{ZW}64;alice"),   # with the "#" broken too, #64 links nothing
    ("cc &commat;bob", f"cc &{ZW}commat;bob"),
    ("see &num;3 and &#x23;4", f"see &{ZW}num;3 and &{ZW}#x23;4"),
    ("&lt;!-- pr-analyzer --&gt;", f"&{ZW}lt;!-- pr-analyzer --&{ZW}gt;"),
])
def test_character_references_are_shown_as_written(text, expected):
    assert neutralise(text) == expected

def test_file_names_in_notes_cannot_mention_or_cross_reference():
    # the review's notes name files the PR chose, as plain text
    note = "Skipped 1 file(s) matching ignore_paths: &commat;acme/GH-1.py"
    assert neutralise_line(note) == f"Skipped 1 file(s) matching ignore_paths: &{ZW}commat;acme/GH-{ZW}1.py"

# ── code is left as it is ─────────────────────────────────────────────────────
# GitHub shows code exactly as written, so nothing in it is live, and a zero-width space in it
# broke copying the code and GitHub's "Commit suggestion"
SUGGESTION = (
    "Index the first row directly:\n"
    "```suggestion\n"
    "row = cur.execute(QUERY, (name,)).fetchall()[0]\n"
    'url = "https://api.example.com/v1/users"  # ask @owner about <b> and #12\n'
    "```"
)

def test_fenced_code_is_left_as_it_is():
    assert neutralise(SUGGESTION) == SUGGESTION

@pytest.mark.parametrize("text", [
    "~~~\n<div>@{user}</div>\n~~~",       # tildes
    "````\n```\n@still.code\n````",       # a shorter fence doesn't close a longer one
    "```\r\n@x\r\n```",                   # Windows line endings
    "```\r@x\r```",                       # and a lone \r, which ends a line too
    "```js\nconst first = rows[0]",       # an unclosed block runs to the end
    "    ```\n```\n@a\n```",              # four spaces make indented code, not a fence
])
def test_other_fenced_code_is_left_as_it_is(text):
    assert neutralise(text) == text

@pytest.mark.parametrize("language", ["c++", "c#", "objective-c", "vb.net"])
def test_code_in_a_language_named_with_punctuation_is_left_as_it_is(language):
    text = f"```{language}\nx = y[0]  // @todo\n```"
    assert neutralise(text) == text

@pytest.mark.parametrize("blank", ["", "  ", "\t"])
def test_code_after_a_blank_line_is_left_as_it_is(blank):
    # a blank line, of nothing but spaces and tabs, ends the paragraph: a backtick left open
    # before it can't pair with the code after it
    text = f"`a\n{blank}\n`@b`"
    assert neutralise(text) == text

def test_text_around_fenced_code_is_still_neutralised():
    text = "Ask @alice:\n\n```python\nx = y[0]  # @todo\n```\nthen @bob"
    expected = f"Ask @{ZW}alice:\n\n```python\nx = y[0]  # @todo\n```\nthen @{ZW}bob"
    assert neutralise(text) == expected

@pytest.mark.parametrize("text, expected", [
    ("Use `@functools.lru_cache` here", "Use `@functools.lru_cache` here"),
    ("Return `items[0] if items else None`", "Return `items[0] if items else None`"),
    ("Use `<details>`, not <details>", f"Use `<details>`, not <{ZW}details>"),
    ("Ask @alice about ``a`b@c``", f"Ask @{ZW}alice about ``a`b@c``"),   # a longer fence
    ("see `GH-1` and `&#x40;`", "see `GH-1` and `&#x40;`"),
    ("`@a\\` @b", f"`@a\\` @{ZW}b"),          # in a code span a backslash escapes nothing
    ("run `ssh $USER@host`", "run `ssh $USER@host`"),   # a "$" in the code itself starts no maths
    ("| a | `@b` |", "| a | `@b` |"),           # a table cell can hold code, if it holds no "|"
])
def test_inline_code_is_left_as_it_is(text, expected):
    assert neutralise(text) == expected
    assert neutralise_line(text) == expected

def test_inline_code_cannot_hide_a_link_definition():
    # GitHub reads [label]: url before it looks for code spans, and [label] then links to url
    text = "[`a]: https://evil.example`\n\nclick [`a]"
    assert neutralise(text) == f"[`a]{ZW}: https://evil.example`\n\nclick [`a]"
    assert neutralise_line("[`a]: x`") == f"[`a]{ZW}: x`"

# ── text that only looks like code ────────────────────────────────────────────
# whatever GitHub wouldn't show as code is neutralised as before
@pytest.mark.parametrize("text, expected", [
    ("\\`@a`", f"\\`@{ZW}a`"),                      # an escaped backtick opens nothing
    ("`` @a `", f"`` @{ZW}a `"),                     # runs of different lengths don't pair
    ("`a\n\n@b`", f"`a\n\n@{ZW}b`"),                 # a code span can't cross a blank line
    ("`a\r\r@b`", f"`a\r\r@{ZW}b`"),                 # a lone \r ends a line as well
    ("`a\n\u00a0\n`@b`", f"`a\n\u00a0\n`@{ZW}b`"),   # a line of non-breaking spaces isn't blank
    ("x ` y\n`@a`", f"x ` y\n`@{ZW}a`"),             # the backtick on the first line pairs first
    ("| `a | @b` |\n| --- |", f"| `a | @{ZW}b` |\n| --- |"),   # a table splits cells at "|"
    ("<1`x@a> `", f"<1`x@{ZW}a> `"),                # an e-mail autolink comes before code
])
def test_look_alike_inline_code_is_neutralised(text, expected):
    assert neutralise(text) == expected

# cmark-gfm, which renders GitHub's comments, opens no code span with a run of more than 1000
# backticks. The "a" after the run reads as a language name, so the line can't pass for a drawn
# block, which would be neutralised whatever the limit.
@pytest.mark.parametrize("ticks, inside", [
    (1000, "a @b "),          # a code span: left as it is
    (1001, f"a @{ZW}b "),     # one more backtick, and it's text
])
def test_runs_of_over_1000_backticks_open_no_code_span(ticks, inside):
    run = "`" * ticks
    assert neutralise(f"{run}a @b {run}") == f"{run}{inside}{run}"
    assert neutralise_line(f"{run}a @b {run}") == f"{run}{inside}{run}"

# ── maths and diagrams ────────────────────────────────────────────────────────
# GitHub draws these instead of showing them as code, and a drawing could hold a link
@pytest.mark.parametrize("text, expected", [
    ("```math\n`\\href{https://x}{y}`\n```", f"```math\n`\\href{{https:{ZW}//x}}{{y}}`\n```"),
    ("~~~ Mermaid\nA[`@b`]\n~~~", f"~~~ Mermaid\nA[`@{ZW}b`]\n~~~"),
    ("```m&#97;th\n`@b`\n```", f"```m&{ZW}#{ZW}97;th\n`@{ZW}b`\n```"),   # GitHub decodes it
    ("- ~~~math\n  `@b`", f"- ~~~math\n  `@{ZW}b`"),         # where the block ends is unclear
    ("- ```math\n\n  `@b`", f"- ```math\n\n  `@{ZW}b`"),     # with backticks, past a blank line
    ("```math\n@b\n```\n`@c`", f"```math\n@{ZW}b\n```\n`@c`"),   # code after the block is code
])
def test_drawn_blocks_are_neutralised(text, expected):
    assert neutralise(text) == expected

@pytest.mark.parametrize("language", ["math", "mermaid", "geojson", "topojson", "stl"])
def test_every_kind_of_drawn_block_is_neutralised(language):
    assert neutralise(f"```{language}\n`@b`\n```") == f"```{language}\n`@{ZW}b`\n```"

@pytest.mark.parametrize("text, expected", [
    ("$`\\href{https://evil.example}{x}`$", f"$`\\href{{https:{ZW}//evil.example}}{{x}}`$"),
    ("costs $5, and `@b`", f"costs $5, and `@{ZW}b`"),
    ("`$x` or `@b`", f"`$x` or `@{ZW}b`"),
    ("$$\n`@b`\n$$", f"$$\n`@{ZW}b`\n$$"),
])
def test_code_after_a_dollar_sign_is_neutralised(text, expected):
    # $`...`$, and code inside $...$ or $$...$$, can be maths
    assert neutralise(text) == expected

@pytest.mark.parametrize("text, expected", [
    ("```js `x`\n@a\n```", f"```js `x`\n@{ZW}a\n```"),         # a backtick in the info string
    ("```\n@a\n````\n@b", f"```\n@a\n````\n@{ZW}b"),             # a longer fence closes too
    ("```\nx\n   ```\n@a", f"```\nx\n   ```\n@{ZW}a"),            # so does one indented 3 spaces
    ("```\nx\n```\t\n@a", f"```\nx\n```\t\n@{ZW}a"),              # or followed by a tab
    ("```\nx\n    ```\n```\n@a", f"```\nx\n    ```\n```\n@{ZW}a"),   # but not one indented 4
    ("\t```\n@a", f"\t```\n@{ZW}a"),                              # a tab indents it too far
    ("> ```\n> @a\n@b", f"> ```\n> @{ZW}a\n@{ZW}b"),              # a quote's block ends with it
    ("- item\n  ```\n@a\n```", f"- item\n  ```\n@{ZW}a\n```"),     # so does a list item's
    ("  ```\n```\n@a\n```", f"  ```\n```\n@{ZW}a\n```"),          # top level or list item?
    ("   ```\nx\n```\n@a\n```", f"   ```\nx\n```\n@{ZW}a\n```"),   # 3 spaces leave the same doubt
    ("   ~~~\nx\n~~~\n@a\n~~~", f"   ~~~\nx\n~~~\n@{ZW}a\n~~~"),   # with tildes
    ("  ```js\nx\n```\n@a\n```", f"  ```js\nx\n```\n@{ZW}a\n```"),   # and with a language
])
def test_look_alike_fenced_code_is_neutralised(text, expected):
    assert neutralise(text) == expected

def test_one_line_text_has_no_code_block():
    # one-line text goes into a list item or a table cell, where a fence starts no code block
    assert neutralise_line("```\n@a") == f"``` @{ZW}a"
    assert neutralise_line("```\n@a\n```") == "``` @a ```"   # but this is a code span

# ── against a CommonMark parser ───────────────────────────────────────────────
# GitHub renders comments with cmark-gfm; markdown-it-py, which Rich installs, follows the same
# CommonMark rules. No "[": after a failed link-label lookahead, markdown-it-py's backtick cache
# drops code spans that CommonMark (and cmark-gfm) keep.
PIECES = [
    "`", "``", "```", "~~~", "\\", "\\`", "@a", "#1", "<a", "<!--", "](", "://", "www.", "&#x40;",
    "&commat;", "&#35;", "GH-1", "gh\\-2", "a", " ", "   ", "\t", "\n", "\n\n", "\r\n", "\r", "- ",
    "> ", "1. ", "|", "| --- |", "\n```\n", "\n  ```\n", "\n   ```\n", "\n~~~math\n", "$", "$`",
    "`$", "<1", "<`", "@a>",
]
LIVE_TEXT = re.compile(r"@[a-z0-9]|#\d|gh-\d|://|www\.", re.IGNORECASE)
LIVE_TOKENS = {"link_open", "image", "html_inline", "html_block"}

def is_live(token, shown_as_text: bool) -> bool:
    return token.type in LIVE_TOKENS or (shown_as_text and bool(LIVE_TEXT.search(token.content)))

def live_parts(md, markdown: str) -> list[str]:
    """Links, images and HTML, and text holding a mention, reference or bare URL. Code that
    GitHub draws, in a maths block or after a "$", counts as text."""
    found = []
    for block in md.parse(markdown):
        if is_live(block, block.type == "fence" and block.info.split()[:1] == ["math"]):
            found.append(f"{block.type}: {block.content}")
        dollar = False   # a "$" earlier in the paragraph
        for token in block.children or []:
            if is_live(token, token.type == "text" or (token.type == "code_inline" and dollar)):
                found.append(f"{token.type}: {token.content}")
            dollar = dollar or "$" in token.content
    return found

def test_nothing_neutralised_is_live_for_a_commonmark_parser():
    markdown_it = pytest.importorskip("markdown_it")
    md = markdown_it.MarkdownIt("commonmark").enable("table")
    rng = random.Random(0)
    for _ in range(2000):
        text = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 24)))
        comment = f"**HIGH** — {neutralise_line(text)}\n\n{neutralise(text)}"
        notes = f"**Notes:**\n- {neutralise_line(text)}\n- end"
        assert live_parts(md, comment) + live_parts(md, notes) == [], repr(text)

# ── code spans ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("app.py:3", "`app.py:3`"),
    ("a`b.py", "``a`b.py``"),             # a backtick in a file name can't close the span
    ("`x`", "`` `x` ``"),
    ("two\nlines.py", "`two lines.py`"),
])
def test_code_span(text, expected):
    assert code_span(text) == expected
