"""The diff lines kept with each finding: parsing, the window around the line, and masking."""
from conftest import make_finding, make_outcome, make_result
from pr_analyzer.models import FileChange

from review_app.snippets import MASK, diff_rows, redact, snippet
from review_app.worker import analysis_summary

PATCH = """@@ -1,4 +1,6 @@
 import os
-PASSWORD = os.environ["PASSWORD"]
+PASSWORD = "hunter2-very-secret"
+TIMEOUT = 30

 def connect():
@@ -40,3 +42,4 @@ def query(sql):
     cursor = db.cursor()
+    cursor.execute("SELECT * FROM users WHERE id = " + user_id)
     return cursor
\\ No newline at end of file"""


def test_diff_rows_number_the_new_file():
    rows = diff_rows(PATCH)
    assert rows[1:6] == [(" ", 1, "import os"), ("-", None, 'PASSWORD = os.environ["PASSWORD"]'),
                         ("+", 2, 'PASSWORD = "hunter2-very-secret"'), ("+", 3, "TIMEOUT = 30"),
                         (" ", 4, "")]
    assert ("+", 43, '    cursor.execute("SELECT * FROM users WHERE id = " + user_id)') in rows
    assert [kind for kind, _, _ in rows].count("@") == 2
    assert not any(text.startswith("No newline") for _, _, text in rows)


def test_the_window_around_a_finding():
    rows = snippet(PATCH, 3, radius=2)
    assert [(r["kind"], r["line"], r["hit"]) for r in rows] == [
        ("-", None, False), ("+", 2, False), ("+", 3, True), (" ", 4, False), (" ", 5, False)]


def test_the_window_stays_in_its_hunk():
    rows = snippet(PATCH, 43, radius=3)
    assert [r["line"] for r in rows] == [42, 43, 44]       # the hunk header above is left out
    assert rows[1]["hit"] and "user_id" in rows[1]["text"]


def test_no_window_without_the_line():
    assert snippet(PATCH, 20) == []        # an unchanged line the diff doesn't show
    assert snippet(None, 3) == [] and snippet(PATCH, None) == []


def test_secrets_never_reach_the_snippet():
    rows = snippet(PATCH, 2)
    password = next(r for r in rows if r["line"] == 2)
    assert "hunter2" not in password["text"] and password["text"] == f'PASSWORD = "{MASK}"'
    assert redact("key = AKIAABCDEFGHIJKLMNOP") == f"key = {MASK}"
    assert redact("token: ghp_" + "a" * 36) == f"token: {MASK}"
    assert redact("x = 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC'") == f"x = '{MASK}'"
    assert redact("total = price * quantity") == "total = price * quantity"


def test_each_stored_finding_keeps_its_diff():
    changed = FileChange(filename="src/db.py", status="modified", additions=3, deletions=1,
                         patch=PATCH)
    result = make_result(files=[changed], suggestions=[
        make_finding("HIGH", title="SQL injection", filename="src/db.py", line=43),
        make_finding("LOW", title="Elsewhere", filename="src/db.py", line=20),
        make_finding("LOW", title="Other file", filename="README.md", line=1),
    ])
    stored = {f["title"]: f["snippet"] for f in analysis_summary(make_outcome(result))["findings"]}
    assert [r["line"] for r in stored["SQL injection"] if r["hit"]] == [43]
    assert stored["Elsewhere"] == [] and stored["Other file"] == []


def test_long_lines_are_clipped():
    patch = "@@ -0,0 +1 @@\n+" + "x" * 500
    [row] = snippet(patch, 1)
    assert len(row["text"]) <= 180
