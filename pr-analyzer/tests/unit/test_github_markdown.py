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
    ("[docs]: https://evil.example", f"[docs]{ZW}: https:{ZW}//evil.example"), # reference links
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

@pytest.mark.parametrize("text", ["a && b, ?x=1&y=2, AT&T", "C# and #define"])
def test_text_that_can_start_nothing_is_unchanged(text):
    assert neutralise(text) == text and neutralise_line(text) == text

def test_text_still_reads_the_same():
    text = "Ask @alice about <b>[this](http://x)</b> in #4, GH-5 or www.example.org &#x40;bob"
    assert neutralise(text).replace(ZW, "") == text

def test_neutralising_twice_changes_nothing_more():
    once = neutralise("@a <b> [c](d) e://f www.g #1 GH-2 &#64;h")
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

# ── code spans ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("app.py:3", "`app.py:3`"),
    ("a`b.py", "``a`b.py``"),             # a backtick in a file name can't close the span
    ("`x`", "`` `x` ``"),
    ("two\nlines.py", "`two lines.py`"),
])
def test_code_span(text, expected):
    assert code_span(text) == expected
