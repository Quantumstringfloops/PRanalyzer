import re

import pytest
from tree_sitter import Parser

from pr_analyzer.analyzer.ast_analyzer import (
    PATTERNS,
    RULE_IDS,
    StaticAnalyzer,
    Thresholds,
    count_complexity,
    get_language,
    iter_functions,
    language_name,
)
from pr_analyzer.models import FileChange

SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

def make_file(patch: str, filename: str = "test.py", raw_content: str | None = None) -> FileChange:
    return FileChange(
        filename=filename,
        status="modified",
        additions=1,
        deletions=0,
        patch=patch,
        raw_content=raw_content,
    )

def added_file(code: str, filename: str = "app.py") -> FileChange:
    # a new file: every line is added and the full content matches the patch
    lines = code.splitlines()
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)
    return make_file(patch, filename, raw_content=code)

def branches(n: int) -> str:
    # a function with n if-statements: cyclomatic complexity n + 1
    body = "\n".join(f"    if x == {i}:\n        y = {i}" for i in range(n))
    return f"def busy(x):\n    y = 0\n{body}\n    return y\n"

def complexity_findings(results):
    return [s for s in results if s.category == "complexity"]

def parse_first_function(code: str, filename: str):
    language = language_name(filename)
    root = Parser(get_language(filename)).parse(code.encode()).root_node
    return next(iter_functions(root, language)), language

analyzer = StaticAnalyzer()

# ── TODO pattern ──────────────────────────────────────────────────────────────
def test_todo_match():
    file = make_file("@@ -1,1 +1,1 @@\n+# TODO: fix this later")
    results = analyzer.analyze([file])
    assert any(s.title == "TODO/FIXME comment added" for s in results)

def test_todo_no_match():
    file = make_file("@@ -1,1 +1,1 @@\n+x = 1")
    results = analyzer.analyze([file])
    assert results == []

# ── removed file skipped ──────────────────────────────────────────────────────
def test_removed_file_skipped():
    file = make_file("@@ -1,1 +1,1 @@\n+# TODO fix", filename="old.py")
    file.status = "removed"
    results = analyzer.analyze([file])
    assert results == []

# ── no patch skipped ──────────────────────────────────────────────────────────
def test_no_patch_skipped():
    file = make_file(None)
    file.patch = None
    results = analyzer.analyze([file])
    assert results == []

# ── line numbers ──────────────────────────────────────────────────────────────
def test_line_number_is_the_added_line():
    # the hunk starts at new line 10 and the TODO is its second line (line numbers were off by one)
    file = make_file("@@ -10,2 +10,3 @@ def handler():\n     x = 1\n+    # TODO handle errors\n     return x")
    [result] = analyzer.analyze([file])
    assert result.line == 11

def test_removed_lines_do_not_shift_line_numbers():
    file = make_file("@@ -1,3 +1,2 @@\n-old = 1\n keep = 2\n+# FIXME")
    [result] = analyzer.analyze([file])
    assert result.line == 2

def test_unchanged_lines_are_not_flagged():
    # the old regexes let \s* run across the newline into an unchanged `except:` line
    file = make_file("@@ -1,1 +1,2 @@\n+\n except:")
    assert analyzer.analyze([file]) == []

# ── language-aware rules ──────────────────────────────────────────────────────
def test_print_flagged_in_python_only():
    python = make_file("@@ -0,0 +1,1 @@\n+print(x)", filename="app.py")
    markdown = make_file("@@ -0,0 +1,1 @@\n+print(x)", filename="README.md")
    assert any(s.title == "print() left in code" for s in analyzer.analyze([python]))
    assert analyzer.analyze([markdown]) == []

def test_method_named_print_not_flagged():
    file = make_file("@@ -0,0 +1,1 @@\n+report.print(x)", filename="app.py")
    assert all(s.title != "print() left in code" for s in analyzer.analyze([file]))

def test_commented_out_code_not_flagged():
    file = make_file("@@ -0,0 +1,1 @@\n+# print(x)", filename="app.py")
    assert analyzer.analyze([file]) == []

def test_named_constant_is_not_a_magic_number():
    constant = make_file("@@ -0,0 +1,1 @@\n+TIMEOUT_SECONDS = 30", filename="app.py")
    literal = make_file("@@ -0,0 +1,1 @@\n+wait(30)", filename="app.py")
    assert analyzer.analyze([constant]) == []
    assert any(s.title == "Possible magic number" for s in analyzer.analyze([literal]))

def test_missing_return_type_not_flagged_in_tests():
    # every test function used to get its own comment, burying the findings that matter
    source = make_file("@@ -0,0 +1,1 @@\n+def handler(x):", filename="app.py")
    test = make_file("@@ -0,0 +1,1 @@\n+def test_handler():", filename="tests/test_app.py")
    assert any(s.title == "Function missing return type hint" for s in analyzer.analyze([source]))
    assert analyzer.analyze([test]) == []

def test_hardcoded_secret_left_to_security_scanner():
    # the static and security scanners both used to flag this, penalising it twice
    file = make_file('@@ -0,0 +1,1 @@\n+password = "hunter22"', filename="app.py")  # pr-analyzer: ignore
    assert analyzer.analyze([file]) == []

# ── tree-sitter complexity ────────────────────────────────────────────────────
def test_complex_function_flagged_at_its_def_line():
    file = added_file(branches(16))   # complexity 17

    [result] = complexity_findings(analyzer.analyze([file]))

    assert result.severity == "MEDIUM"
    assert result.line == 1
    assert "busy" in result.description

def test_very_complex_function_is_high():
    # this branch used to be unreachable because the > 20 check came first
    file = added_file(branches(31))   # complexity 32
    [result] = complexity_findings(analyzer.analyze([file]))
    assert result.severity == "HIGH"

def test_simple_function_not_flagged():
    assert complexity_findings(analyzer.analyze([added_file(branches(3))])) == []

def test_untouched_complex_function_not_reported():
    code = branches(20) + "\n\ndef other():\n    return 1\n"
    last = len(code.splitlines())
    # the PR only changes `other`, at the end of the file
    file = make_file(f"@@ -{last},1 +{last},1 @@\n-    return 0\n+    return 1", "app.py", code)
    assert complexity_findings(analyzer.analyze([file])) == []

def test_python_complexity_counts_each_decision():
    code = "def f(a, b, c):\n    if a and b:\n        pass\n    elif c:\n        pass\n    return 1 if a else 2\n"
    function, language = parse_first_function(code, "app.py")
    assert count_complexity(function, language) == 4   # if, and, elif, conditional expression

def test_js_complexity_counts_logical_operators():
    code = "function f(a, b) { if (a && b || a) { return 1; } return a ? 1 : 2; }"
    function, language = parse_first_function(code, "app.js")
    assert count_complexity(function, language) == 4   # if, &&, ||, ternary

def test_nested_functions_measured_separately():
    code = "def outer(a):\n    def inner(b):\n        if b:\n            return 1\n    return inner\n"
    function, language = parse_first_function(code, "app.py")
    assert count_complexity(function, language) == 0   # the `if` belongs to inner

@pytest.mark.parametrize("filename", ["app.js", "component.jsx", "app.ts", "view.tsx"])
def test_js_and_ts_files_do_not_crash(filename):
    # .js/.jsx used to raise AttributeError: 'Language' object has no attribute 'value'
    assert get_language(filename) is not None
    analyzer.analyze([added_file("const f = (a) => a && a.b;\n", filename)])

def test_severities_are_valid():
    # a misspelt severity ("MEDUIM") used to crash the scorer
    assert {p["severity"] for p in PATTERNS} <= SEVERITIES
    assert {s.severity for s in analyzer.analyze([added_file(branches(31))])} <= SEVERITIES

# ── strings and comments ──────────────────────────────────────────────────────
def rules_for(line: str, filename: str = "app.py") -> list[str]:
    return [s.rule_id for s in analyzer.analyze([make_file(f"@@ -0,0 +1,1 @@\n+{line}", filename)])]

@pytest.mark.parametrize("line, filename", [
    ('log.info("call print(x) to see it")', "app.py"),
    ("x = 1  # print(x) while debugging", "app.py"),
    ('"""Use print(x) to debug."""', "app.py"),
    ('const hint = "use console.log(x)";', "app.js"),
    ("run();  // console.log(x)", "app.js"),
    ("run(); /* console.log(x) */", "app.ts"),
])
def test_calls_inside_strings_and_comments_are_not_flagged(line, filename):
    assert rules_for(line, filename) == []

def test_a_call_after_a_string_is_still_flagged():
    assert rules_for('label = "#"; print(label)') == ["print-call"]

def test_todo_counts_in_comments_but_not_in_strings():
    assert rules_for('status = "TODO"') == []
    assert rules_for('label = f"TODO: {name}"') == []
    assert rules_for("x = 1  # TODO: tidy") == ["todo-comment"]
    assert rules_for("run();  // FIXME later", "app.js") == ["todo-comment"]
    assert rules_for("run(); /* TODO */", "app.ts") == ["todo-comment"]

@pytest.mark.parametrize("line, filename", [
    ("    TODO: support the v2 format.", "parse.py"),            # a docstring's middle line
    ('"""TODO: document this."""', "parse.py"),                  # a one-line docstring
    ('    """Parse the header. TODO: handle v2.', "parse.py"),   # a docstring's first line
    ("   FIXME: this races with the reconnect", "socket.js"),    # inside a /* */ comment
    ("case '\"': quoted = !quoted; // TODO: escapes", "lexer.c"),   # after a character literal
])
def test_todo_in_docstrings_and_comment_bodies(line, filename):
    # only # and // comments used to count, so these notes went unreported
    assert rules_for(line, filename) == ["todo-comment"]

# ── magic numbers ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("line, filename", [
    ("requests.get(url, timeout=25)", "app.py"),         # a keyword argument names it
    ("def fetch(url, retries=25) -> None:", "app.py"),   # so does a default value
    ("limit = 25", "app.py"),                             # and an assignment
    ('settings = {"retries": 25}', "app.py"),             # and a dict key
    ("const options = { retries: 25 };", "app.js"),       # and an object property
    ("_LIMIT = 25", "app.py"),                            # a private constant is a constant too
    ("time.sleep(60)", "app.py"),                         # well-known values
    ("size = chunk * 1024", "app.py"),
    ("abort(404)", "app.py"),
    ("res.status(418).end();", "app.js"),
    ("prefix = line[:25]", "app.py"),                     # slices and indexes
    ("last = parts[-12]", "app.py"),
    ("if sys.version_info >= (3, 11):", "app.py"),         # a version check names its numbers
    ('log.info("retried 25 times")', "app.py"),           # numbers in strings and comments
    ("retry()  # at most 25 times", "app.py"),
    ('label = f"{name:>25}"', "app.py"),                  # and in a format spec
])
def test_explained_numbers_are_not_magic(line, filename):
    assert "magic-number" not in rules_for(line, filename)

@pytest.mark.parametrize("line", [
    "wait(25)",
    "if attempts > 25:",
    "return 42",
    "sizes = [25, 50]",      # a list literal isn't an index
    "if count == 25:",       # a comparison doesn't name its number
    "total += 25",
    'msg = f"{total * 25} left"',   # the code in an f-string's field is code
])
def test_unexplained_numbers_are_still_flagged(line):
    assert "magic-number" in rules_for(line)

# ── C-family and PHP ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("line, filename", [
    ("#define CACHE_TTL_SECONDS 90", "cache.h"),   # the named constant the rule asks for
    ("#if defined(_MSC_VER) && _MSC_VER >= 1900", "compat.h"),
    ("#pragma warning disable 1591", "Api.cs"),
])
def test_preprocessor_lines_are_not_magic_numbers(line, filename):
    assert "magic-number" not in rules_for(line, filename)

def test_php_hash_comment_is_a_comment():
    assert rules_for("# TODO: remove this hack", "index.php") == ["todo-comment"]

# ── rule ids ──────────────────────────────────────────────────────────────────
def test_rule_ids_are_unique_and_kebab_case():
    # they are what .pr-analyzer.toml's `disable` list names, so they must stay stable
    assert len(RULE_IDS) == len(set(RULE_IDS))
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)*", rule) for rule in RULE_IDS)

def test_findings_carry_their_rule_id():
    file = make_file("@@ -0,0 +1,2 @@\n+# TODO: fix this later\n+" + "x" * 130)
    assert [s.rule_id for s in analyzer.analyze([file])] == ["todo-comment", "long-line"]

def test_disabled_rules_are_skipped():
    file = make_file("@@ -0,0 +1,2 @@\n+print(x)  # TODO\n+wait(30)", filename="app.py")
    found = analyzer.analyze([file], disabled={"print-call", "magic-number"})
    assert [s.rule_id for s in found] == ["todo-comment"]

def test_tree_sitter_checks_can_be_disabled():
    file = added_file(branches(31) + "x = 1\n" * 500)
    rules = {s.rule_id for s in analyzer.analyze([file], disabled={"complexity", "file-length"})}
    assert not rules & {"complexity", "file-length"}

# ── thresholds ────────────────────────────────────────────────────────────────
def test_line_length_limit_is_configurable():
    file = make_file("@@ -0,0 +1,1 @@\n+" + "x" * 90, filename="app.py")
    assert analyzer.analyze([file]) == []
    [result] = analyzer.analyze([file], thresholds=Thresholds(max_line_length=80))
    assert (result.rule_id, result.title) == ("long-line", "Line exceeds 80 characters")

def test_complexity_thresholds_are_configurable():
    file = added_file(branches(6))   # complexity 7
    assert complexity_findings(analyzer.analyze([file])) == []

    [medium] = complexity_findings(analyzer.analyze([file], thresholds=Thresholds(complexity_medium=5)))
    [high] = complexity_findings(
        analyzer.analyze([file], thresholds=Thresholds(complexity_medium=4, complexity_high=6))
    )

    assert (medium.severity, medium.rule_id) == ("MEDIUM", "complexity")
    assert "above 5" in medium.description
    assert high.severity == "HIGH"

def test_file_already_too_long_is_not_blamed_on_the_pr():
    # a long file used to be reported on every PR that touched it, whatever the change
    code = "x = 1\n" * 30
    file = make_file("@@ -30,0 +31,1 @@\n+y = 2", "app.py", code + "y = 2\n")
    assert analyzer.analyze([file], thresholds=Thresholds(max_file_lines=10)) == []

def test_pr_that_makes_a_file_too_long_is_told():
    code = "x = 1\n" * 12
    file = make_file("@@ -9,0 +10,3 @@\n+x = 1\n+x = 1\n+x = 1", "app.py", code)
    [result] = analyzer.analyze([file], thresholds=Thresholds(max_file_lines=10))
    assert result.rule_id == "file-length"
    assert "from 9 to 12 lines, past the limit of 10" in result.description

def test_file_length_limit_is_configurable():
    file = added_file("x = 1\n" * 20)
    assert analyzer.analyze([file]) == []
    [result] = analyzer.analyze([file], thresholds=Thresholds(max_file_lines=10))
    assert (result.rule_id, result.title) == ("file-length", "File too long")
