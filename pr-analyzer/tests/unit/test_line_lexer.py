import pytest

from pr_analyzer.analyzer.line_lexer import LineParts, blank_string, split_line

def blanks(n: int) -> str:
    return " " * n

# ── Python ────────────────────────────────────────────────────────────────────
def test_python_string_contents_are_blanked_but_quotes_kept():
    parts = split_line('x = b"SELECT id" + y', ".py")
    assert parts.code == f'x = b"{blanks(9)}" + y'
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
    assert split_line('x = """Call print(x) to debug."""', ".py").code == f'x = """{blanks(23)}"""'

@pytest.mark.parametrize("line", [
    '"""Call print(x) to debug."""', '    """Parse the header.', "    r'''TODO: v2.'''  # noqa",
])
def test_a_line_holding_only_a_docstring_is_a_comment(line):
    # a docstring is a note, not data: a to-do marker in it was missed
    parts = split_line(line, ".py")
    assert parts.code.strip() == "" and parts.comments == line

@pytest.mark.parametrize("line", ['"""a""" + b', 'f"""{x}"""', '"""a""""'])
def test_a_string_that_only_starts_the_line_is_no_docstring(line):
    assert split_line(line, ".py").comments.strip() == ""

def test_unclosed_string_runs_to_the_end_of_the_line():
    # the first line of a multi-line string: the rest of the line is text
    assert split_line('q = """SELECT eval(x)', ".py").code == f'q = """{blanks(14)}'   # pr-analyzer: ignore

@pytest.mark.parametrize("line, extension", [
    ('a = "b"  # c', ".py"), ('f"{a:{b}} {c!r}"  # d', ".py"), ("`a ${b}` // c", ".js"),
])
def test_views_keep_the_line_length(line, extension):
    parts = split_line(line, extension)
    assert len(parts.code) == len(parts.comments) == len(line)

# ── f-strings and template literals ───────────────────────────────────────────
@pytest.mark.parametrize("line, code", [
    # the text is blanked and the code in a field kept: it used to be blanked with the string
    ('x = f"SELECT {id}" + y', 'x = f"' + blanks(7) + '{id}" + y'),
    ('otp = f"{random.randint(0, 999999)}"', 'otp = f"{random.randint(0, 999999)}"'),  # pr-analyzer: ignore
    # a format spec is text, apart from a field nested in it
    ('f"{n:>8} {x:.{prec}f}"', 'f"{n' + blanks(3) + "} {x" + blanks(2) + "{prec}" + blanks(1) + '}"'),
    ('f"{{x}} {y}"', 'f"' + blanks(6) + '{y}"'),          # {{ and }} are braces, not fields
    ('f"{d["key"]}"', 'f"{d["' + blanks(3) + '"]}"'),      # a string in a field is still a string
    ('f"{ {1: a}[b[1:2]] }"', 'f"{ {1: a}[b[1:2]] }"'),    # brackets don't end a field
    # a backslash doesn't escape a brace, and \N{...} names a character unless the string is raw
    (r'rf"\{x}" + f"\N{BULLET} {y}"', 'rf" {x}" + f"' + blanks(11) + '{y}"'),
    ('f"""a "b" {c}"""', 'f"""' + blanks(6) + '{c}"""'),
    ('t"<p>{name}</p>"', 't"' + blanks(3) + "{name}" + blanks(4) + '"'),   # a t-string
    ('if"{x}": pass', 'if"   ": pass'),                   # `if` is no f prefix
])
def test_fstring_fields_are_code(line, code):
    assert split_line(line, ".py").code == code

@pytest.mark.parametrize("line, code", [
    ("const s = `call ${a}`;", "const s = `" + blanks(5) + "${a}`;"),
    ("`a ${b ? `c${d}` : 'e'} f`", "`  ${b ? ` ${d}` : ' '}  `"),   # templates nest
    ("`\\${a} $b ${ {c: 1}.c }`", "`" + blanks(9) + "${ {c: 1}.c }`"),
])
def test_template_literal_fields_are_code(line, code):
    assert split_line(line, ".ts").code == code

def test_a_comment_inside_a_field_is_a_comment():
    parts = split_line("`${/* why */ x}`", ".js")
    assert (parts.code.strip(), parts.comments.strip()) == ("`${" + blanks(9) + " x}`", "/* why */")

@pytest.mark.parametrize("line, extension", [('f"{' * 1000 + "x", ".py"), ("`${" * 1000, ".js")])
def test_deeply_nested_fields_do_not_exhaust_the_stack(line, extension):
    assert len(split_line(line, extension).code) == len(line)

# ── JavaScript / TypeScript ───────────────────────────────────────────────────
@pytest.mark.parametrize("line, code, comments", [
    ("foo();  // console.log(x)", "foo();", "// console.log(x)"),
    ("/* eval(x) */ run()", blanks(14) + "run()", "/* eval(x) */"),   # pr-analyzer: ignore
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

def test_c_family_character_literals_are_strings():
    # '"' used to open a string that ran to the end of the line and hid the comment
    parts = split_line("case '\"': quoted = !quoted; // TODO: escapes", ".c")
    assert parts.code.rstrip() == "case ' ': quoted = !quoted;"
    assert parts.comments.strip() == "// TODO: escapes"

@pytest.mark.parametrize("line, extension", [
    ("#define CACHE_TTL_SECONDS 90", ".h"),
    ("  #pragma warning disable 1591", ".cs"),
    ("#if defined(_MSC_VER) && _MSC_VER >= 1900", ".cpp"),
])
def test_c_preprocessor_directives_are_comments(line, extension):
    # they were read as code, so every numeric #define counted as a magic number
    parts = split_line(line, extension)
    assert parts.code.strip() == "" and parts.comments == line

def test_rust_attributes_are_code():
    assert split_line("#[repr(align(64))]", ".rs").code == "#[repr(align(64))]"

def test_php_is_matched_as_before():
    # read as C, the // in a '...' string started a comment and a # comment was code
    url = "$x = file_get_contents('http://a'); eval($x);"   # pr-analyzer: ignore
    assert split_line(url, ".php") == LineParts(url, url)
    assert split_line("# never eval($input)", ".php").code.strip() == ""   # pr-analyzer: ignore

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
