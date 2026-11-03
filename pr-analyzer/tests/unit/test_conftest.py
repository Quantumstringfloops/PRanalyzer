"""The suite's own isolation (tests/conftest.py): every test starts from the default settings,
whatever the developer's .env file and environment say."""
import os
import subprocess
import sys

from pr_analyzer.config import Settings, settings

# what a .env made from .env.example might hold after a few edits, and what a shell might export:
# valid values, none of them the default
DOTENV = """\
CACHE_ENABLED=false
GITHUB_MAX_WORKERS=1
GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS=0
LLM_TOKEN_BUDGET=100
LLM_MAX_CONCURRENCY=4
CACHE_MAX_SIZE_MB=1
"""
ENVIRONMENT = {
    "LLM_PROVIDER": "anthropic",
    "LLM_MODEL": "some-model",
    "MAX_DIFF_LINES": "7",
    "LLM_CONFIDENCE_THRESHOLD": "0.95",
    "GITHUB_TOKEN": "not-a-real-token",
    "CACHE_DIR": "/nonexistent/pr-analyzer-cache",
    "log_level": "DEBUG",   # names are matched ignoring case
}


# ── the settings a test starts from ───────────────────────────────────────────
def test_settings_are_the_defaults(tmp_path):
    expected = {name: field.get_default(call_default_factory=True)
                for name, field in Settings.model_fields.items()}
    expected["cache_dir"] = str(tmp_path / "pr-analyzer-cache")   # isolated_cache's

    assert {name: getattr(settings, name) for name in expected} == expected

def test_no_setting_is_left_in_the_environment():
    assert [name for name in os.environ if name.lower() in Settings.model_fields] == []

def test_the_developers_env_file_and_environment_change_nothing(tmp_path):
    # the two tests above, in a pytest of their own, run from a directory whose .env sets the
    # settings the suite depends on, while the shell exports more
    (tmp_path / ".env").write_text(DOTENV)
    probes = [f"{__file__}::{test.__name__}"
              for test in (test_settings_are_the_defaults, test_no_setting_is_left_in_the_environment)]

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "basetemp"), *probes],
        cwd=tmp_path, env={**os.environ, **ENVIRONMENT}, capture_output=True, text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
