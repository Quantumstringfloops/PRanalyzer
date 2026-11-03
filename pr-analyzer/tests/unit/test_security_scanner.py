import pytest
from pr_analyzer.security.scanner import SecurityScanner
from pr_analyzer.models import FileChange, Suggestion

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

def titles(file: FileChange) -> list[str]:
    return [s.title for s in scanner.scan([file])]

scanner = SecurityScanner()

# ── clean diff ────────────────────────────────────────────────────────────────
def test_clean_diff_returns_empty():
    file = make_file(added("x = 1 + 2"))
    assert scanner.scan([file]) == []

# ── deleted line never flagged ────────────────────────────────────────────────
def test_deleted_line_not_flagged():
    file = make_file('@@ -1,1 +1,1 @@\n-password = "secret123"')  # pr-analyzer: ignore
    assert scanner.scan([file]) == []

# ── secret patterns ───────────────────────────────────────────────────────────
def test_aws_key():
    file = make_file(added('key = "AKIAIOSFODNN7EXAMPLE"'))  # pr-analyzer: ignore
    assert any(s.title == "AWS access key exposed" for s in scanner.scan([file]))

def test_aws_key_no_match():
    file = make_file(added('key = "notanawskey"'))
    assert scanner.scan([file]) == []

def test_generic_api_key():
    file = make_file(added('api_key = "abcdefghijklmnop"'))  # pr-analyzer: ignore
    assert any(s.title == "Hardcoded API key" for s in scanner.scan([file]))

def test_generic_api_key_no_match():
    file = make_file(added('api_key = "short"'))
    assert scanner.scan([file]) == []

def test_private_pem():
    file = make_file(added("-----BEGIN RSA PRIVATE KEY-----"))  # pr-analyzer: ignore
    assert any(s.title == "Private key exposed" for s in scanner.scan([file]))

def test_openssh_private_key():
    file = make_file(added("-----BEGIN OPENSSH PRIVATE KEY-----"))  # pr-analyzer: ignore
    assert "Private key exposed" in titles(file)

def test_github_pat():
    file = make_file(added('token = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"'))  # pr-analyzer: ignore
    assert any(s.title == "GitHub personal access token exposed" for s in scanner.scan([file]))

def test_github_fine_grained_token():
    file = make_file(added('token = "github_pat_11ABCDEFG0123456789_abcdefghijklmnop"'))  # pr-analyzer: ignore
    assert "GitHub personal access token exposed" in titles(file)

def test_jwt_token():
    file = make_file(added('token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"'))  # pr-analyzer: ignore
    assert any(s.title == "JWT token hardcoded" for s in scanner.scan([file]))

def test_hardcoded_password():
    file = make_file(added('password = "supersecret"'))  # pr-analyzer: ignore
    assert any(s.title == "Hardcoded password" for s in scanner.scan([file]))

def test_hardcoded_password_no_match():
    file = make_file(added('x = "hello"'))
    assert scanner.scan([file]) == []

@pytest.mark.parametrize("value", ["changeme", "<your-password>", "${DB_PASSWORD}", "********"])
def test_placeholder_password_not_flagged(value):
    assert scanner.scan([make_file(added(f'password = "{value}"'))]) == []

def test_db_connection_string():
    file = make_file(added('db = "postgres://user:pass@localhost/mydb"'))  # pr-analyzer: ignore
    assert any(s.title == "Database connection string with credentials" for s in scanner.scan([file]))

@pytest.mark.parametrize("url", ["postgresql://app:pw@db/main", "mongodb+srv://app:pw@cluster0.example.net"])  # pr-analyzer: ignore
def test_db_connection_string_other_schemes(url):
    assert "Database connection string with credentials" in titles(make_file(added(f'DB = "{url}"')))

def test_db_url_without_credentials_not_flagged():
    # the old regex matched across lines, from a credential-free URL to a later @decorator
    patch = '@@ -1,1 +1,3 @@\n+REDIS_URL = "redis://localhost:6379/0"\n+\n+@app.route("/")'
    assert scanner.scan([make_file(patch)]) == []

def test_secrets_checked_without_file_content():
    # files whose full content couldn't be fetched used to be skipped entirely
    file = make_file(added('password = "supersecret"'))  # pr-analyzer: ignore
    file.raw_content = None
    assert "Hardcoded password" in titles(file)

# ── dangerous patterns ────────────────────────────────────────────────────────
def test_eval():
    file = make_file(added("eval(user_input)"))  # pr-analyzer: ignore
    assert any(s.title == "eval() usage detected" for s in scanner.scan([file]))  # pr-analyzer: ignore

def test_eval_no_match():
    file = make_file(added("x = 1"))
    assert scanner.scan([file]) == []

@pytest.mark.parametrize("code", ["model.eval()", "# never call eval() here", "score = my_eval(x)"])  # pr-analyzer: ignore
def test_eval_false_positives(code):
    assert scanner.scan([make_file(added(code))]) == []

def test_pickle_load():
    file = make_file(added("pickle.load(f)"))  # pr-analyzer: ignore
    assert any(s.title == "pickle.load() usage detected" for s in scanner.scan([file]))  # pr-analyzer: ignore

def test_pickle_loads():
    assert "pickle.load() usage detected" in titles(make_file(added("obj = pickle.loads(blob)")))  # pr-analyzer: ignore

def test_yaml_unsafe_load():
    file = make_file(added("yaml.load(data)"))  # pr-analyzer: ignore
    assert any(s.title == "yaml.load() without SafeLoader" for s in scanner.scan([file]))  # pr-analyzer: ignore

def test_yaml_safe_loader_not_flagged():
    assert scanner.scan([make_file(added("yaml.load(data, Loader=yaml.SafeLoader)"))]) == []

def test_subprocess_shell():
    file = make_file(added("subprocess.run(cmd, shell=True)"))  # pr-analyzer: ignore
    assert any(s.title == "subprocess with shell=True" for s in scanner.scan([file]))

@pytest.mark.parametrize("code", [
    'cur.execute(f"SELECT * FROM users WHERE id = {uid}")',  # pr-analyzer: ignore
    'cur.execute("SELECT * FROM users WHERE id = %s" % uid)',  # pr-analyzer: ignore
    'cur.execute("SELECT * FROM users WHERE id = {}".format(uid))',  # pr-analyzer: ignore
    'cur.execute("SELECT * FROM users WHERE id = " + uid)',  # pr-analyzer: ignore
])
def test_sql_built_with_string_formatting(code):
    assert "Possible SQL injection" in titles(make_file(added(code)))

def test_parameterised_sql_not_flagged():
    # the safe form the rule recommends used to be reported as HIGH
    assert scanner.scan([make_file(added('cur.execute("SELECT * FROM users WHERE id = %s", (uid,))'))]) == []

def test_random_for_secrets():
    file = make_file(added("token = random.randint(0, 999999)"))  # pr-analyzer: ignore
    assert any(s.title == "Non-cryptographic random used" for s in scanner.scan([file]))

def test_random_for_non_secret_values_not_flagged():
    assert scanner.scan([make_file(added("delay = random.random()"))]) == []

def test_debug_mode():
    file = make_file(added("DEBUG = True"))  # pr-analyzer: ignore
    assert any(s.title == "Debug mode enabled" for s in scanner.scan([file]))

def test_debug_mode_no_match():
    file = make_file(added("DEBUG = False"))
    assert scanner.scan([file]) == []

def test_dangerous_patterns_only_in_source_files():
    docs = make_file(added("Never call eval(input) on user data."), filename="README.md")  # pr-analyzer: ignore
    assert scanner.scan([docs]) == []

# ── line numbers ──────────────────────────────────────────────────────────────
def test_line_numbers_point_at_the_added_line():
    patch = '@@ -1,2 +1,3 @@\n import os\n x = 1\n+password = "supersecret"'  # pr-analyzer: ignore
    [result] = scanner.scan([make_file(patch)])
    assert result.line == 3

def test_same_rule_on_two_lines_reported_twice():
    # wrong line numbers used to give both the same line, and dedup then dropped one
    patch = "@@ -10,2 +10,4 @@\n a = 1\n+eval(x)\n b = 2\n+eval(y)"  # pr-analyzer: ignore
    assert [s.line for s in scanner.scan([make_file(patch)])] == [11, 13]

# ── deduplication ─────────────────────────────────────────────────────────────
def test_no_duplicate_findings():
    file = make_file(added('password = "secret123"'))  # pr-analyzer: ignore
    results = scanner.scan([file])
    titles = [s.title for s in results]
    assert len(titles) == len(set(titles))

# ── skip logic ────────────────────────────────────────────────────────────────
def test_removed_file_skipped():
    file = make_file(added('password = "secret"'), status="removed")  # pr-analyzer: ignore
    assert scanner.scan([file]) == []

def test_no_patch_skipped():
    file = make_file(None)
    file.patch = None
    assert scanner.scan([file]) == []

# ── external tools ────────────────────────────────────────────────────────────
class FakeRunner:
    def __init__(self, findings=(), warnings=()):
        self.calls = 0
        self.findings = list(findings)
        self.warnings = list(warnings)

    def scan(self, files):
        self.calls += 1
        return self.findings

def test_external_tools_run_once_per_pr():
    # semgrep and pip-audit used to run once for every file in the PR
    semgrep, auditor = FakeRunner(), FakeRunner()
    files = [make_file(added("x = 1"), filename=f"f{i}.py") for i in range(3)]

    SecurityScanner(semgrep=semgrep, dep_auditor=auditor).scan(files)

    assert (semgrep.calls, auditor.calls) == (1, 1)

def test_tool_findings_and_warnings_are_collected():
    finding = Suggestion(filename="a.py", severity="HIGH", category="security",
                         title="Semgrep: eval-detected", description="d", line=1, source="security")
    scanner_with_tools = SecurityScanner(
        semgrep=FakeRunner(findings=[finding]),
        dep_auditor=FakeRunner(warnings=["Dependency audit skipped: pip-audit is not installed"]),
    )

    results = scanner_with_tools.scan([make_file(added("x = 1"), filename="a.py")])

    assert results == [finding]
    assert scanner_with_tools.warnings == ["Dependency audit skipped: pip-audit is not installed"]
