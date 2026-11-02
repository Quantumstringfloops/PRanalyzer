import pytest

from pr_analyzer.security import tools


@pytest.fixture(autouse=True)
def no_external_tools(monkeypatch):
    # Tests must never start the real semgrep or pip-audit: they're slow and need the network.
    # The runner tests stub subprocess.run and point find_tool at a fake executable themselves.
    monkeypatch.setattr(tools, "find_tool", lambda name: None)
