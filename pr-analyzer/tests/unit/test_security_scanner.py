import pytest
from unittest.mock import MagicMock
from pr_analyzer.security.scanner import SecurityScanner
from pr_analyzer.models import FileChange

def make_file(patch: str, status: str = "modified") -> FileChange:
    return FileChange(
        filename="test.py",
        status=status,
        additions=1,
        deletions=0,
        patch=patch,
        raw_content="x = 1",
    )

scanner = SecurityScanner()

# ── secret patterns ───────────────────────────────────────────────────────────
def test_aws_key_match():
    file = make_file("@@ -1,1 +1,1 @@\n+key = AKIAIOSFODNN7EXAMPLE")
    results = scanner.scan([file])
    assert any(s.title == "AWS access key exposed" for s in results)

def test_aws_key_no_match():
    file = make_file("@@ -1,1 +1,1 @@\n+x = 1")
    results = scanner.scan([file])
    assert results == []

def test_hardcoded_password_match():
    file = make_file('@@ -1,1 +1,1 @@\n+password = "secret123"')
    results = scanner.scan([file])
    assert any(s.title == "Hardcoded password" for s in results)

def test_deleted_line_not_flagged():
    file = make_file('@@ -1,1 +1,1 @@\n-password = "secret123"')
    results = scanner.scan([file])
    assert results == []

# ── dangerous patterns ────────────────────────────────────────────────────────
def test_eval_match():
    file = make_file("@@ -1,1 +1,1 @@\n+eval(user_input)")
    results = scanner.scan([file])
    assert any(s.title == "eval() usage detected" for s in results)

def test_eval_no_match():
    file = make_file("@@ -1,1 +1,1 @@\n+x = 1")
    results = scanner.scan([file])
    assert results == []

def test_debug_mode_match():
    file = make_file("@@ -1,1 +1,1 @@\n+DEBUG = True")
    results = scanner.scan([file])
    assert any(s.title == "Debug mode enabled" for s in results)

# ── skip logic ────────────────────────────────────────────────────────────────
def test_removed_file_skipped():
    file = make_file('@@ -1,1 +1,1 @@\n+password = "secret"', status="removed")
    results = scanner.scan([file])
    assert results == []

def test_no_patch_skipped():
    file = make_file(None)
    file.patch = None
    results = scanner.scan([file])
    assert results == []