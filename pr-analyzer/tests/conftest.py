import os

import pytest
import structlog

from pr_analyzer.config import Settings, settings
from pr_analyzer.security import tools


@pytest.fixture(autouse=True)
def no_external_tools(monkeypatch):
    # Tests must never start the real semgrep or pip-audit: they're slow and need the network.
    # The runner tests stub subprocess.run and point find_tool at a fake executable themselves.
    monkeypatch.setattr(tools, "find_tool", lambda name: None)


@pytest.fixture(autouse=True)
def reset_logging():
    # the CLI points structlog at the stderr of the test runner that invoked it; undo that so
    # later tests don't log into a stream that no longer exists
    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    # Every test starts from the default settings, whatever the developer's .env file and
    # environment say: pr_analyzer.config read both on import, and the tests run from where the
    # README has them copy .env.example to .env. The variables go too, for whatever reads them
    # again. A test that needs another value sets it.
    for name in list(os.environ):
        if name.lower() in Settings.model_fields:   # names are matched ignoring case
            monkeypatch.delenv(name)
    defaults = Settings(_env_file=None)
    for name in Settings.model_fields:
        monkeypatch.setattr(settings, name, getattr(defaults, name))


@pytest.fixture(autouse=True)
def isolated_cache(default_settings, tmp_path, monkeypatch):
    # tests must never read or write the real cache in ~/.cache, nor share entries between tests
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "pr-analyzer-cache"))
