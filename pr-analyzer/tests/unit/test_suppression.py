from pr_analyzer.models import FileChange, Suggestion
from pr_analyzer.suppression import ignored_warning, marked_lines, split_ignored

def make_file(content, filename="app.py", patch=None):
    return FileChange(filename=filename, status="modified", additions=1, deletions=0,
                      patch=patch, raw_content=content)

def finding(line, filename="app.py"):
    return Suggestion(filename=filename, severity="HIGH", category="security",
                      title="Risky call", description="d", line=line, source="security")

# ── finding the marker ────────────────────────────────────────────────────────
def test_marked_lines_from_file_content():
    content = "x = 1\nrun(cmd)  # pr-analyzer: ignore\ncall()  // pr-analyzer:ignore\ny = 2"
    assert marked_lines(make_file(content)) == {2, 3}

def test_marked_lines_fall_back_to_the_patch():
    # the full content isn't always available
    patch = "@@ -1,1 +1,2 @@\n x = 1\n+run(cmd)  # pr-analyzer: ignore"
    assert marked_lines(make_file(None, patch=patch)) == {2}

def test_similar_words_are_not_the_marker():
    assert marked_lines(make_file("x = 1  # pr-analyzer: ignored\ny = 2  # ignore")) == set()

def test_line_numbers_match_the_diff():
    # str.splitlines() also splits on form feeds, which would shift every later line
    assert marked_lines(make_file("a = 1\f\nrun(cmd)  # pr-analyzer: ignore")) == {2}

# ── splitting findings ────────────────────────────────────────────────────────
def test_only_findings_on_marked_lines_are_ignored():
    files = [make_file("run(a)  # pr-analyzer: ignore\nrun(b)"), make_file("run(c)", "b.py")]
    on_marker, plain, no_line, other_file = finding(1), finding(2), finding(None), finding(1, "b.py")

    kept, ignored = split_ignored([on_marker, plain, no_line, other_file], files)

    assert kept == [plain, no_line, other_file]
    assert ignored == [on_marker]

def test_findings_for_files_outside_the_pr_are_kept():
    stray = finding(1, "elsewhere.py")
    assert split_ignored([stray], [make_file("x  # pr-analyzer: ignore")]) == ([stray], [])

# ── the warning ───────────────────────────────────────────────────────────────
def test_warning_names_each_ignored_line_once():
    warning = ignored_warning([finding(12), finding(3), finding(3)])
    assert warning == "Ignored 3 finding(s) on lines marked 'pr-analyzer: ignore': app.py:3, app.py:12"

def test_warning_caps_the_list():
    warning = ignored_warning([finding(n) for n in range(1, 15)])
    assert warning.endswith("app.py:10 and 4 more")
