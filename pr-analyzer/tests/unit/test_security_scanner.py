import pytest
from pr_analyzer.security.scanner import SecurityScanner
from pr_analyzer.models import FileChange

def make_file(patch: str, status: str = "modified", filename: str = "test.py") -> FileChange:
    return FileChange(
        filename=filename,
        status=status,
        additions=1,
        deletions=0,
        patch=patch,
        raw_content="x = 1",
    )

def added(code: str) -> str:
    return f"@@ -1,1 +1,1 @@\n+{code}"

scanner = SecurityScanner()

# ── clean diff ────────────────────────────────────────────────────────────────
def test_clean_diff_returns_empty():
    file = make_file(added("x = 1 + 2"))
    assert scanner.scan([file]) == []

# ── deleted line never flagged ────────────────────────────────────────────────
def test_deleted_line_not_flagged():
    file = make_file('@@ -1,1 +1,1 @@\n-password = "secret123"')
    assert scanner.scan([file]) == []

# ── secret patterns ───────────────────────────────────────────────────────────
def test_aws_key():
    file = make_file(added('key = "AKIAIOSFODNN7EXAMPLE"'))
    assert any(s.title == "AWS access key exposed" for s in scanner.scan([file]))

def test_aws_key_no_match():
    file = make_file(added('key = "notanawskey"'))
    assert scanner.scan([file]) == []

def test_generic_api_key():
    file = make_file(added('api_key = "abcdefghijklmnop"'))
    assert any(s.title == "Hardcoded API key" for s in scanner.scan([file]))

def test_generic_api_key_no_match():
    file = make_file(added('api_key = "short"'))
    assert scanner.scan([file]) == []

def test_private_pem():
    file = make_file(added("-----BEGIN RSA PRIVATE KEY-----"))
    assert any(s.title == "Private key exposed" for s in scanner.scan([file]))

def test_github_pat():
    file = make_file(added('token = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"'))
    assert any(s.title == "GitHub personal access token exposed" for s in scanner.scan([file]))

def test_jwt_token():
    file = make_file(added('token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"'))
    assert any(s.title == "JWT token hardcoded" for s in scanner.scan([file]))

def test_hardcoded_password():
    file = make_file(added('password = "supersecret"'))
    assert any(s.title == "Hardcoded password" for s in scanner.scan([file]))

def test_hardcoded_password_no_match():
    file = make_file(added('x = "hello"'))
    assert scanner.scan([file]) == []

def test_db_connection_string():
    file = make_file(added('db = "postgres://user:pass@localhost/mydb"'))
    assert any(s.title == "Database connection string with credentials" for s in scanner.scan([file]))

# ── dangerous patterns ────────────────────────────────────────────────────────
def test_eval():
    file = make_file(added("eval(user_input)"))
    assert any(s.title == "eval() usage detected" for s in scanner.scan([file]))

def test_eval_no_match():
    file = make_file(added("x = 1"))
    assert scanner.scan([file]) == []

def test_pickle_load():
    file = make_file(added("pickle.load(f)"))
    assert any(s.title == "pickle.load() usage detected" for s in scanner.scan([file]))

def test_yaml_unsafe_load():
    file = make_file(added("yaml.load(data)"))
    assert any(s.title == "yaml.load() without SafeLoader" for s in scanner.scan([file]))

def test_subprocess_shell():
    file = make_file(added("subprocess.run(cmd, shell=True)"))
    assert any(s.title == "subprocess with shell=True" for s in scanner.scan([file]))

def test_random_for_secrets():
    file = make_file(added("token = random.randint(0, 999999)"))
    assert any(s.title == "Non-cryptographic random used" for s in scanner.scan([file]))

def test_debug_mode():
    file = make_file(added("DEBUG = True"))
    assert any(s.title == "Debug mode enabled" for s in scanner.scan([file]))

def test_debug_mode_no_match():
    file = make_file(added("DEBUG = False"))
    assert scanner.scan([file]) == []

# ── deduplication ─────────────────────────────────────────────────────────────
def test_no_duplicate_findings():
    file = make_file(added('password = "secret123"'))
    results = scanner.scan([file])
    titles = [s.title for s in results]
    assert len(titles) == len(set(titles))

# ── skip logic ────────────────────────────────────────────────────────────────
def test_removed_file_skipped():
    file = make_file(added('password = "secret"'), status="removed")
    assert scanner.scan([file]) == []

def test_no_patch_skipped():
    file = make_file(None)
    file.patch = None
    assert scanner.scan([file]) == []