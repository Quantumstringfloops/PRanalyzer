from pr_analyzer.diff import annotate_patch, commentable_lines, iter_added_lines, iter_new_lines

PATCH = "\n".join([
    "@@ -10,4 +10,5 @@ def handler():",
    "     a = 1",
    "-    b = 2",
    "+    b = 3",
    "+    c = 4",
    "",
    "\\ No newline at end of file",
    "@@ -40,2 +41,2 @@",
    " x",
    "+y",
])

def test_iter_new_lines_numbers_lines_in_the_new_file():
    assert list(iter_new_lines(PATCH)) == [
        (10, "    a = 1", False),
        (11, "    b = 3", True),
        (12, "    c = 4", True),
        (13, "", False),
        (41, "x", False),
        (42, "y", True),
    ]

def test_iter_added_lines_only_yields_additions():
    assert list(iter_added_lines(PATCH)) == [(11, "    b = 3"), (12, "    c = 4"), (42, "y")]

def test_commentable_lines_cover_context_and_additions():
    assert commentable_lines(PATCH) == {10, 11, 12, 13, 41, 42}

def test_text_before_first_hunk_is_ignored():
    patch = "diff --git a/x b/x\n+++ b/x\n@@ -1,0 +1,1 @@\n+z"
    assert list(iter_new_lines(patch)) == [(1, "z", True)]

def test_annotate_patch_numbers_new_lines():
    lines = annotate_patch("@@ -1,2 +1,2 @@\n-old\n+new\n same").splitlines()
    assert lines == ["@@ -1,2 +1,2 @@", "      -old", "    1 +new", "    2  same"]
