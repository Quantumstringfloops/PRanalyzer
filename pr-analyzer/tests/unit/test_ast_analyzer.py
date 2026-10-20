from src.pr_analyzer.analyzer.ast_analyzer import StaticAnalyzer
from src.pr_analyzer.models import FileChange

def make_file(patch: str, filename: str = "test.py") -> FileChange:
    return FileChange(
        filename=filename,
        status="modified",
        additions=1,
        deletions=0,
        patch=patch,
    )

analyzer = StaticAnalyzer()

# ── TODO pattern ──────────────────────────────────────────────────────────────
def test_todo_match():
    file = make_file("@@ -1,1 +1,1 @@\n+# TODO: fix this later")
    results = analyzer.analyze([file])
    assert any(s.title == "TODO comment" for s in results)

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