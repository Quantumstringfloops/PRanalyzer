import pytest
import structlog

from pr_analyzer.config import settings
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
def isolated_cache(tmp_path, monkeypatch):
    # tests must never read or write the real cache in ~/.cache, nor share entries between tests
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "pr-analyzer-cache"))
