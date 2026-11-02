import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from pr_analyzer.models import FileChange
from pr_analyzer.security import semgrep_runner, tools
from pr_analyzer.security.semgrep_runner import SemgrepRunner

# app.py gains lines 2-3; line 5 was already there before the PR
APP_PATCH = "@@ -1,3 +1,5 @@\n x = 1\n+eval(a)\n+print(a)\n y = 2\n eval(old)"

def make_file(filename, patch=APP_PATCH, status="modified", content="x = 1\n"):
    return FileChange(filename=filename, status=status, additions=1, deletions=0,
                      patch=patch, raw_content=content)

def sarif(results, rules=()):
    return json.dumps({"runs": [{"tool": {"driver": {"rules": list(rules)}}, "results": results}]})

def result(rule_id, uri, line, end=None):
    region = {"startLine": line, "endLine": end or line}
    return {
        "ruleId": rule_id,
        "message": {"text": f"{rule_id} found"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": region}}],
    }

def rule(rule_id, level):
    return {"id": rule_id, "defaultConfiguration": {"level": level}}

def written_files(root):
    found = []
    for directory, _, names in os.walk(root):
        found += [os.path.relpath(os.path.join(directory, n), root) for n in names]
    return sorted(found)

@pytest.fixture
def fake_semgrep(monkeypatch):
    """Pretend semgrep is installed; record each run and answer with canned output."""
    monkeypatch.setattr(tools, "find_tool", lambda name: f"/fake/bin/{name}")
    calls = []

    def install(stdout="", returncode=0, raises=None):
        def run(command, **kwargs):
            calls.append(SimpleNamespace(command=command, kwargs=kwargs,
                                         files=written_files(kwargs["cwd"])))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="boom")
        monkeypatch.setattr(semgrep_runner.subprocess, "run", run)
        return calls

    return install

def test_scans_every_changed_file_in_one_run(fake_semgrep, monkeypatch):
    # it used to scan only the last file, once per file in the PR
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    calls = fake_semgrep(stdout=sarif([]))
    files = [make_file("app.py"), make_file("web/ui.js"), make_file("old.py", status="removed")]

    SemgrepRunner(config="p/test").scan(files)

    [call] = calls
    assert call.files == ["app.py", os.path.join("web", "ui.js")]
    assert call.command[-1] == "."
    assert ["--metrics", "off"] == call.command[call.command.index("--metrics"):][:2]
    assert "GITHUB_TOKEN" not in call.kwargs["env"]

def test_findings_use_real_filenames_and_rule_severity(fake_semgrep):
    # findings used to be labelled 'fake_file.py' and were always MEDIUM
    fake_semgrep(stdout=sarif(
        [result("python.eval-detected", "web/ui.js", 2), result("python.print-call", "app.py", 3)],
        rules=[rule("python.eval-detected", "error"), rule("python.print-call", "note")],
    ))

    found = SemgrepRunner().scan([make_file("app.py"), make_file("web/ui.js")])

    assert [(s.filename, s.line, s.severity, s.title) for s in found] == [
        ("web/ui.js", 2, "HIGH", "Semgrep: eval-detected"),
        ("app.py", 3, "LOW", "Semgrep: print-call"),
    ]

def test_findings_on_unchanged_lines_dropped(fake_semgrep):
    fake_semgrep(stdout=sarif([result("eval-detected", "app.py", 5)], [rule("eval-detected", "error")]))
    assert SemgrepRunner().scan([make_file("app.py")]) == []

def test_last_file_removed_does_not_crash(fake_semgrep):
    # writing the removed file's missing content used to raise TypeError
    fake_semgrep(stdout=sarif([]))
    files = [make_file("app.py"), make_file("gone.py", status="removed", content=None)]
    assert SemgrepRunner().scan(files) == []

def test_paths_outside_the_scan_directory_are_not_written(fake_semgrep):
    calls = fake_semgrep(stdout=sarif([]))
    SemgrepRunner().scan([make_file("app.py"), make_file("../escape.py")])
    assert calls[0].files == ["app.py"]

def test_missing_semgrep_is_skipped_with_warning():
    runner = SemgrepRunner()
    assert runner.scan([make_file("app.py")]) == []
    assert "not installed" in runner.warnings[0]

@pytest.mark.parametrize("kwargs, expected", [
    ({"raises": subprocess.TimeoutExpired("semgrep", 1)}, "took longer"),
    ({"returncode": 2}, "exit 2"),
    ({"stdout": "not json"}, "not valid SARIF"),
])
def test_tool_failures_become_warnings(fake_semgrep, kwargs, expected):
    fake_semgrep(**kwargs)
    runner = SemgrepRunner()
    assert runner.scan([make_file("app.py")]) == []
    assert expected in runner.warnings[0]
