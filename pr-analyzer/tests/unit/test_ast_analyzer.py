import pytest
from tree_sitter import Parser

from pr_analyzer.analyzer.ast_analyzer import (
    PATTERNS,
    StaticAnalyzer,
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
    file = make_file('@@ -0,0 +1,1 @@\n+password = "hunter22"', filename="app.py")
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
