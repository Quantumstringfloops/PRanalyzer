"""PrAnalyzer: pr-analyzer's pipeline run with an installation token, plus what the decision
needs beyond its result."""
from types import SimpleNamespace

import pytest
import requests
from conftest import NEW_SHA, SHA, make_pull, make_result
from github import GithubException

from pr_analyzer.exceptions import PRAnalyzerError
from pr_analyzer.security import tools

from review_app import analysis
from review_app.analysis import PrAnalyzer


class FakePipeline:
    instances: list["FakePipeline"] = []
    config: bytes | None = None
    files: list = []
    head = SHA
    list_error: Exception | None = None
    run_error: Exception | None = None

    def __init__(self, skip_llm, post_review, github_token):
        self.args = {"skip_llm": skip_llm, "post_review": post_review, "token": github_token}
        self.closed = False
        self.github = SimpleNamespace(git_instance=SimpleNamespace(get_repo=self._repo),
                                      fetch_file=self._fetch_file)
        FakePipeline.instances.append(self)

    def run(self, url):
        self.url = url
        if FakePipeline.run_error is not None:
            raise FakePipeline.run_error
        return make_result()

    def close(self):
        self.closed = True

    def _repo(self, name):
        def get_pull(number):
            if FakePipeline.list_error is not None:
                raise FakePipeline.list_error
            return SimpleNamespace(get_files=lambda: FakePipeline.files,
                                   head=SimpleNamespace(sha=FakePipeline.head))
        return SimpleNamespace(get_pull=get_pull)

    def _fetch_file(self, repo, path, ref):
        assert (repo, path, ref) == ("octo/app", ".pr-analyzer.toml", "main")
        return FakePipeline.config


def github_file(name, status="modified", changes=3, patch="@@ +1 @@", previous=None):
    return SimpleNamespace(filename=name, status=status, changes=changes, patch=patch,
                           previous_filename=previous)


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    monkeypatch.setattr(analysis, "AnalysisPipeline", FakePipeline)
    monkeypatch.setattr(tools, "find_tool", lambda name: f"/usr/local/bin/{name}")
    FakePipeline.instances = []
    FakePipeline.config = None
    FakePipeline.files = [github_file("src/app.py"),
                          github_file("logo.png", "added", 0, patch=None)]
    FakePipeline.head = SHA
    FakePipeline.list_error = None
    FakePipeline.run_error = None


def test_runs_the_pipeline_with_the_installation_token():
    outcome = PrAnalyzer().analyze(make_pull(), token="ghs_x", post_review=True, skip_llm=False)

    pipeline = FakePipeline.instances[0]
    assert pipeline.args == {"skip_llm": False, "post_review": True, "token": "ghs_x"}
    assert pipeline.url == "https://github.com/octo/app/pull/7"
    assert pipeline.closed
    assert [(f.filename, f.has_patch) for f in outcome.changed_files] == [
        ("src/app.py", True), ("logo.png", False)]
    assert outcome.missing_tools == ()
    assert outcome.disabled_stages == ()
    assert not outcome.head_moved


def test_reports_missing_scanners(monkeypatch):
    monkeypatch.setattr(tools, "find_tool", lambda name: None if name == "pip-audit" else name)
    outcome = PrAnalyzer().analyze(make_pull(), token=None, post_review=False, skip_llm=True)
    assert outcome.missing_tools == ("pip-audit",)


def test_notices_new_commits_during_the_analysis():
    FakePipeline.head = NEW_SHA
    assert PrAnalyzer().analyze(make_pull(), token=None, post_review=False,
                                skip_llm=False).head_moved


@pytest.mark.parametrize("error", [GithubException(502, "bad gateway", None),
                                   requests.ConnectionError("reset")])
def test_files_that_cannot_be_listed_are_unknown_not_none(error):
    FakePipeline.list_error = error
    outcome = PrAnalyzer().analyze(make_pull(), token=None, post_review=False, skip_llm=False)
    assert outcome.changed_files is None
    assert not outcome.head_moved


def test_reads_the_stages_the_project_switched_off():
    FakePipeline.config = b'disable = ["semgrep", "llm", "magic-number"]\n'
    outcome = PrAnalyzer().analyze(make_pull(), token=None, post_review=False, skip_llm=False)
    assert outcome.disabled_stages == ("semgrep", "llm")


def test_an_invalid_project_config_switches_nothing_off():
    # pr-analyzer then used its defaults, and said so in a warning
    FakePipeline.config = b'disable = ["no-such-rule"]\n'
    outcome = PrAnalyzer().analyze(make_pull(), token=None, post_review=False, skip_llm=False)
    assert outcome.disabled_stages == ()


def test_the_pipeline_is_closed_when_it_fails():
    FakePipeline.run_error = PRAnalyzerError("GitHub error 500: boom")
    with pytest.raises(PRAnalyzerError):
        PrAnalyzer().analyze(make_pull(), token=None, post_review=False, skip_llm=False)
    assert FakePipeline.instances[0].closed
