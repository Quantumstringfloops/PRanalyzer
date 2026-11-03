import re
import sys
import tomllib
from pathlib import Path

import pytest

from pr_analyzer import cli
from pr_analyzer.exceptions import LLMError
from pr_analyzer.llm.engine import LLMEngine
from pr_analyzer.models import FileChange
from pr_analyzer.security.dep_auditor import DepAuditScanner
from pr_analyzer.security.semgrep_runner import SemgrepRunner

PROJECT_DIR = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = PROJECT_DIR.parent / ".github" / "workflows" / "release.yml"
PYPROJECT = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
NAME = PYPROJECT["project"]["name"]

# projects already on PyPI that belong to someone else
TAKEN = ["pr-analyzer"]

def ultranormalize(name: str) -> str:
    # PyPI refuses a new project whose name matches an existing one's in this form: lowercased,
    # without '.', '_' and '-', and with l and i read as 1 and o as 0
    return re.sub(r"[._-]", "", name.lower()).translate(str.maketrans("lio", "110"))

# ── the name on PyPI ──────────────────────────────────────────────────────────
def test_pypi_accepts_the_distribution_name():
    # "pranalyzer" could never be registered: PyPI reads it as the existing "pr-analyzer"
    assert ultranormalize("pranalyzer") == ultranormalize("pr-analyzer")
    assert ultranormalize(NAME) not in {ultranormalize(taken) for taken in TAKEN}

def test_version_is_looked_up_under_the_distribution_name():
    assert cli.DISTRIBUTION == NAME

def test_command_and_import_package_keep_their_names():
    assert PYPROJECT["project"]["scripts"] == {"pr-analyzer": "pr_analyzer.cli:app"}
    assert PYPROJECT["tool"]["poetry"]["packages"] == [{"include": "pr_analyzer", "from": "src"}]

# ── install hints ─────────────────────────────────────────────────────────────
def missing_semgrep(monkeypatch) -> str:
    runner = SemgrepRunner()   # the conftest hides the real semgrep and pip-audit
    runner.scan([FileChange(filename="app.py", status="added", additions=1, deletions=0,
                            patch="@@ -0,0 +1,1 @@\n+x = 1", raw_content="x = 1\n")])
    return runner.warnings[0]

def missing_pip_audit(monkeypatch) -> str:
    auditor = DepAuditScanner()
    auditor.scan([FileChange(filename="requirements.txt", status="added", additions=1,
                             deletions=0, patch="@@ -0,0 +1,1 @@\n+flask==3.0.0")])
    return auditor.warnings[0]

def missing_sdk(monkeypatch, provider: str) -> str:
    monkeypatch.setitem(sys.modules, provider, None)   # makes `import <provider>` fail
    with pytest.raises(LLMError) as error:
        getattr(LLMEngine(), f"call_{provider}")("prompt")
    return str(error.value)

def extra_packages(extra: str) -> set[str]:
    requirements = PYPROJECT["project"]["optional-dependencies"][extra]
    return {re.split(r"[^\w.-]", requirement, maxsplit=1)[0] for requirement in requirements}

@pytest.mark.parametrize("hint, package", [
    (missing_semgrep, "semgrep"),
    (missing_pip_audit, "pip-audit"),
    (lambda monkeypatch: missing_sdk(monkeypatch, "openai"), "openai"),
    (lambda monkeypatch: missing_sdk(monkeypatch, "anthropic"), "anthropic"),
])
def test_install_hints_name_the_published_package(monkeypatch, hint, package):
    # each hint must install something that exists, with the extra that brings the package
    match = re.search(r"pip install '([\w.-]+)\[(\w+)\]'", hint(monkeypatch))
    assert match is not None
    name, extra = match.groups()
    assert name == NAME
    assert package in extra_packages(extra)

# ── docs and release workflow ─────────────────────────────────────────────────
def test_readme_installs_the_published_package():
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    installed = set(re.findall(r"pipx? install \"?([\w.-]+)", readme))
    # Poetry is the one other thing the README installs, for contributors
    assert installed == {NAME, "poetry"}

def test_release_environments_link_to_the_published_package():
    release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    urls = re.findall(r"^ +url: (\S+)$", release, flags=re.MULTILINE)
    assert urls == [f"https://pypi.org/p/{NAME}", f"https://test.pypi.org/p/{NAME}"]
