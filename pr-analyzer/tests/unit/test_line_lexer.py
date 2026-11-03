import pytest

from pr_analyzer.analyzer.line_lexer import blank_string, split_line

def blanks(n: int) -> str:
    return " " * n

# ── Python ────────────────────────────────────────────────────────────────────
def test_python_string_contents_are_blanked_but_quotes_kept():
    parts = split_line('x = f"SELECT {id}" + y', ".py")
    assert parts.code == f'x = f"{blanks(11)}" + y'
    assert parts.comments.strip() == ""

def test_python_comment_is_split_off():
    parts = split_line('x = 1  # see "eval(" docs', ".py")   # pr-analyzer: ignore
    assert parts.code.rstrip() == "x = 1"
    assert parts.comments.strip() == '# see "eval(" docs'   # pr-analyzer: ignore

def test_hash_inside_a_string_is_not_a_comment():
    parts = split_line('color = "#fff"  # white', ".py")
    assert parts.code.rstrip() == f'color = "{blanks(4)}"'
    assert parts.comments.strip() == "# white"

def test_escaped_quotes_stay_inside_the_string():
    assert split_line(r'msg = "say \"hi\" now"; run()', ".py").code == f'msg = "{blanks(14)}"; run()'

def test_single_line_triple_quoted_string():
    assert split_line('"""Call print(x) to debug."""', ".py").code == f'"""{blanks(23)}"""'

def test_unclosed_string_runs_to_the_end_of_the_line():
    # the first line of a multi-line string: the rest of the line is text
    assert split_line('q = """SELECT eval(x)', ".py").code == f'q = """{blanks(14)}'   # pr-analyzer: ignore

def test_views_keep_the_line_length():
    line = 'a = "b"  # c'
    parts = split_line(line, ".py")
    assert len(parts.code) == len(parts.comments) == len(line)

# ── JavaScript / TypeScript ───────────────────────────────────────────────────
@pytest.mark.parametrize("line, code, comments", [
    ("foo();  // console.log(x)", "foo();", "// console.log(x)"),
    ("/* eval(x) */ run()", blanks(14) + "run()", "/* eval(x) */"),   # pr-analyzer: ignore
    ("const s = `call ${a}`;", f"const s = `{blanks(9)}`;", ""),
    ("const t = 'it\\'s';", f"const t = '{blanks(5)}';", ""),
    ('const url = "http://x"; // home', f'const url = "{blanks(8)}";', "// home"),
])
def test_javascript(line, code, comments):
    parts = split_line(line, ".ts")
    assert parts.code.rstrip() == code
    assert parts.comments.strip() == comments

# ── other languages ───────────────────────────────────────────────────────────
def test_c_family_strings_and_comments():
    parts = split_line('fmt.Println("eval(x)") // done', ".go")   # pr-analyzer: ignore
    assert parts.code.rstrip() == f'fmt.Println("{blanks(7)}")'
    assert parts.comments.strip() == "// done"

def test_rust_lifetimes_are_not_strings():
    assert split_line("fn f<'a>(x: &'a str) {}", ".rs").code == "fn f<'a>(x: &'a str) {}"

def test_unlexed_languages_match_as_before():
    parts = split_line('echo "eval(x)"', ".sh")   # pr-analyzer: ignore
    assert parts.code == parts.comments == 'echo "eval(x)"'   # pr-analyzer: ignore

@pytest.mark.parametrize("line, extension", [("# x", ".py"), ("  * TODO", ".js"), ("// y", ".sh")])
def test_whole_line_comments_in_any_language(line, extension):
    parts = split_line(line, extension)
    assert parts.code.strip() == "" and parts.comments == line

# ── blank_string ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("literal, expected", [
    ('"abc"', '"   "'), ("''", "''"), ('"""x"""', '""" """'), ('"open', '"    '), ('"', '"'),
])
def test_blank_string(literal, expected):
    assert blank_string(literal) == expected
