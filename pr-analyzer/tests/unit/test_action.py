import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# the composite action lives at the repository root, above the Python project
ACTION = Path(__file__).resolve().parents[3] / "action.yml"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="the action's steps are bash scripts")

REPORT = {"score": {"overall": 100, "grade": "A+", "passed": True, "summary": "ok"},
          "suggestions": [], "warnings": []}

# Stands in for the Python the action installs with: it notes where it ran and with which
# arguments, and "-m venv DIR" makes DIR/bin/python, a copy of itself, as a real venv would.
FAKE_PYTHON = """#!/bin/sh
printf '%s\\t%s\\n' "$(pwd -P)" "$*" >> "$CALLS"
for arg in "$@"; do last="$arg"; done
case " $* " in *" -m venv "*) mkdir -p "$last/bin" && cp "$0" "$last/bin/python" ;; esac
"""

# A json.py that a checked-out PR could hold: any Python process that imports it notes where it
# ran, and stops
PLANTED = """import os, sys
with open(os.environ["MARKERS"], "a", encoding="utf-8") as handle:
    handle.write(f"{__name__}.py ran in {sys.argv}\\n")
raise SystemExit(99)
"""

# Stands in for a Python process started without -I, as pip's install of build dependencies and
# the pr-analyzer command are: it notes the names of the PYTHON* variables it got, then imports json
PROBE = """import os
with open(os.environ["SEEN"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sorted(name for name in os.environ if name.startswith("PYTHON"))) + "\\n")
import json
"""

def run_scripts() -> dict[str, str]:
    """The run: script of each step, by step id, unindented as the runner hands it to bash."""
    scripts = {}
    for step in ACTION.read_text(encoding="utf-8").split("\n    - ")[1:]:   # runs.steps
        lines = step.split("\n")
        if not any(line.startswith("      run:") for line in lines):
            continue
        step_id = next(line.split(": ", 1)[1] for line in lines if line.startswith("      id: "))
        block = lines[lines.index("      run: |") + 1:]
        # the literal block ends at the first line indented less than its 8 spaces
        end = next((i for i, line in enumerate(block) if line and not line.startswith(" " * 8)),
                   len(block))
        scripts[step_id] = "\n".join(line[8:] for line in block[:end]).rstrip("\n") + "\n"
    return scripts

def write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path

def run_step(step_id: str, workspace: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    script = workspace.parent / f"{step_id}.sh"
    script.write_text(run_scripts()[step_id], encoding="utf-8")
    # a composite run step starts in the workspace, as `bash --noprofile --norc -eo pipefail`
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=workspace, env={"PATH": os.environ.get("PATH", os.defpath), **env},
        capture_output=True, text=True, timeout=60,
    )

def probe_command(tmp_path: Path) -> str:
    """A shell command that runs PROBE with this Python, without -I, and fails if it fails."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))} || exit"

def job_python_variables(workspace: Path) -> dict[str, str]:
    """PYTHON* variables a job could set, among them a PYTHONPATH that points at the workspace,
    where the PR is checked out."""
    return {"PYTHONPATH": str(workspace), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "PYTHON": "/usr/bin/python3"}

# ── reading action.yml ────────────────────────────────────────────────────────
def test_scripts_are_read_as_a_yaml_parser_reads_them():
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    assert run_scripts() == {step["id"]: step["run"] for step in steps if "run" in step}

def test_no_expression_is_pasted_into_a_script():
    # inputs reach the scripts through env:, so text from the PR can't turn into shell code
    scripts = run_scripts()
    assert set(scripts) == {"install", "analyze"}
    assert [step for step, script in scripts.items() if "${{" in script] == []

# ── the PR's files can't stand in for Python modules ──────────────────────────
@needs_bash
def test_install_step_runs_python_isolated_and_outside_the_workspace(tmp_path):
    # `python -m pip` from the workspace used to run a pip.py (or venv.py) from the PR instead,
    # which could replace pr-analyzer before the next step ran it with the token and API keys
    workspace, runner_temp = tmp_path / "workspace", tmp_path / "runner-temp"
    workspace.mkdir()
    runner_temp.mkdir()
    calls, outputs = tmp_path / "calls", tmp_path / "outputs"
    result = run_step("install", workspace, {
        "PYTHON": str(write_executable(tmp_path / "python", FAKE_PYTHON)),
        "SOURCE": "/action/pr-analyzer", "EXTRAS": "security", "CALLS": str(calls),
        "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(outputs),
    })

    assert result.returncode == 0, result.stderr
    venv = runner_temp / "pr-analyzer-venv"
    assert outputs.read_text() == f"bin={venv}/bin\n"
    ran = [line.split("\t") for line in calls.read_text().splitlines()]
    assert [arguments for _, arguments in ran] == [
        f"-I -m venv {venv}",
        "-I -m pip install --quiet --disable-pip-version-check /action/pr-analyzer[security]",
    ]
    assert not [cwd for cwd, _ in ran if Path(cwd).is_relative_to(workspace.resolve())]

@needs_bash
def test_analyze_step_never_imports_modules_from_its_directory(tmp_path):
    # the step runs from an empty directory, and -I means it doesn't have to stay empty
    workspace, runner_temp, bin_dir = tmp_path / "workspace", tmp_path / "runner-temp", tmp_path / "bin"
    run_dir = runner_temp / "pr-analyzer-run"
    for folder in (workspace, run_dir, bin_dir):
        folder.mkdir(parents=True)
    for module in ("secrets", "json"):
        (run_dir / f"{module}.py").write_text("raise SystemExit('a planted module ran')\n")
    write_executable(bin_dir / "python", f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    write_executable(bin_dir / "pr-analyzer", f"#!/bin/sh\necho {shlex.quote(json.dumps(REPORT))}\n")
    outputs = tmp_path / "outputs"
    result = run_step("analyze", workspace, {
        "PR_ANALYZER_BIN": str(bin_dir), "PR_URL": "https://github.com/o/r/pull/1",
        "POST_REVIEW": "false", "FAIL_ON_BLOCK": "true", "LLM_PROVIDER": "", "CONFIG": "",
        "RUNNER_TEMP": str(runner_temp), "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(outputs),
    })

    assert result.returncode == 0, result.stdout + result.stderr
    assert "a planted module ran" not in result.stderr
    assert outputs.read_text() == "score=100\ngrade=A+\npassed=true\n"

# ── the job's PYTHON* variables reach no Python process ───────────────────────
@needs_bash
def test_processes_pip_starts_get_none_of_the_jobs_python_variables(tmp_path):
    # pip runs with -I, but the Python it starts to install build dependencies doesn't, so a
    # PYTHONPATH the job pointed at the PR used to run the PR's json.py there, able to write to
    # the venv that the next step runs with the token
    workspace, runner_temp = tmp_path / "workspace", tmp_path / "runner-temp"
    workspace.mkdir()
    runner_temp.mkdir()
    (workspace / "json.py").write_text(PLANTED, encoding="utf-8")
    fake_python = FAKE_PYTHON + f'case " $* " in *" -m pip "*) {probe_command(tmp_path)} ;; esac\n'
    markers, seen = tmp_path / "markers", tmp_path / "seen"
    result = run_step("install", workspace, {
        **job_python_variables(workspace),
        "PYTHON": str(write_executable(tmp_path / "python", fake_python)),
        "SOURCE": "/action/pr-analyzer", "EXTRAS": "security", "CALLS": str(tmp_path / "calls"),
        "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "MARKERS": str(markers), "SEEN": str(seen),
    })

    assert not markers.exists(), markers.read_text()
    assert result.returncode == 0, result.stderr
    assert seen.read_text() == "PYTHON\n"   # the step's own, which names the Python to install with

@needs_bash
def test_pr_analyzer_gets_none_of_the_jobs_python_variables(tmp_path):
    # pr-analyzer runs without -I, from its console script, and hands its environment, less its
    # secrets, to Semgrep and pip-audit, so a PYTHONPATH the job pointed at the PR used to run the
    # PR's json.py in all three, and in pr-analyzer with the token in its environment
    workspace, runner_temp = tmp_path / "workspace", tmp_path / "runner-temp"
    bin_dir = tmp_path / "bin"
    for folder in (workspace, runner_temp, bin_dir):
        folder.mkdir()
    (workspace / "json.py").write_text(PLANTED, encoding="utf-8")
    write_executable(bin_dir / "python", f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    write_executable(bin_dir / "pr-analyzer", f"#!/bin/sh\n{probe_command(tmp_path)}\n"
                                              f"echo {shlex.quote(json.dumps(REPORT))}\n")
    markers, seen, outputs = tmp_path / "markers", tmp_path / "seen", tmp_path / "outputs"
    result = run_step("analyze", workspace, {
        **job_python_variables(workspace),
        "PR_ANALYZER_BIN": str(bin_dir), "PR_URL": "https://github.com/o/r/pull/1",
        "POST_REVIEW": "false", "FAIL_ON_BLOCK": "true", "LLM_PROVIDER": "", "CONFIG": "",
        "RUNNER_TEMP": str(runner_temp), "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(outputs), "MARKERS": str(markers), "SEEN": str(seen),
    })

    assert not markers.exists(), markers.read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert seen.read_text() == "\n"   # not even PYTHON, which this step doesn't set
    assert outputs.read_text() == "score=100\ngrade=A+\npassed=true\n"
