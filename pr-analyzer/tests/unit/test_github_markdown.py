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
])
def test_neutralise(text, expected):
    assert neutralise(text) == expected

def test_plain_text_is_unchanged():
    text = "eval() usage detected: avoid eval on user input (rule: python.eval)"   # pr-analyzer: ignore
    assert neutralise(text) == text

def test_text_still_reads_the_same():
    text = "Ask @alice about <b>[this](http://x)</b> in #4 or www.example.org"
    assert neutralise(text).replace(ZW, "") == text

def test_neutralising_twice_changes_nothing_more():
    once = neutralise("@a <b> [c](d) e://f www.g #1")
    assert neutralise(once) == once

def test_one_line_text_loses_its_newlines():
    # a newline in a list item would let the text start its own heading or table
    assert neutralise_line("Risky\n\n## ✅ PASSED\n@x") == f"Risky ## ✅ PASSED @{ZW}x"

# ── code spans ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("app.py:3", "`app.py:3`"),
    ("a`b.py", "``a`b.py``"),             # a backtick in a file name can't close the span
    ("`x`", "`` `x` ``"),
    ("two\nlines.py", "`two lines.py`"),
])
def test_code_span(text, expected):
    assert code_span(text) == expected
