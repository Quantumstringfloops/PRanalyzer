import os

import pytest

from pr_analyzer.config import Settings as AnalyzerSettings
from pr_analyzer.config import settings as analyzer_settings
from pr_analyzer.models import AnalysisResult, FileChange, PRSummary, QualityScore, Suggestion
from pr_analyzer.security import tools

from review_app.db import Store
from review_app.jobs import JobQueue
from review_app.models import AnalysisOutcome, ChangedFile, MergeResult, PullInfo
from review_app.settings import AppSettings

SHA = "a" * 40
NEW_SHA = "b" * 40


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    # pr-analyzer's settings are read once, on import, from the environment and .env: start each
    # test from the defaults, away from any real .env, cache, or scanner
    for name in list(os.environ):
        if name.lower() in AnalyzerSettings.model_fields or name.lower() in AppSettings.model_fields:
            monkeypatch.delenv(name)
    defaults = AnalyzerSettings(_env_file=None)
    for name in AnalyzerSettings.model_fields:
        monkeypatch.setattr(analyzer_settings, name, getattr(defaults, name))
    monkeypatch.setattr(analyzer_settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(tools, "find_tool", lambda name: None)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "review-app.db")
    store.init()
    return store


@pytest.fixture
def queue(store) -> JobQueue:
    return JobQueue(store)


def make_pull(**changes) -> PullInfo:
    values = dict(repo="octo/app", number=7, title="Add a helper", author="dev", head_sha=SHA,
                  base_branch="main", default_branch="main", head_repo_id=1, base_repo_id=1,
                  is_draft=False, state="open", changed_files=1, additions=10, deletions=2)
    values.update(changes)
    return PullInfo(**values)


def make_file(filename: str = "src/app.py", additions: int = 10, deletions: int = 2,
              status: str = "modified", content: str | None = "x = 1\n") -> FileChange:
    return FileChange(filename=filename, status=status, additions=additions, deletions=deletions,
                      patch="@@ -1,2 +1,10 @@\n+x = 1", raw_content=content)


def make_finding(severity: str = "MEDIUM", source: str = "static", confidence: float = 1.0,
                 title: str = "Function too complex", filename: str = "src/app.py",
                 line: int | None = 3) -> Suggestion:
    return Suggestion(filename=filename, severity=severity, category="complexity", title=title,
                      description="Split it up.", line=line, source=source,
                      confidence=confidence, rule_id="complexity")


def make_result(score: float = 95.0, grade: str = "A", passed: bool = True,
                suggestions: list[Suggestion] | None = None, warnings: list[str] | None = None,
                files: list[FileChange] | None = None, llm: str | None = "nvidia",
                head_sha: str = SHA) -> AnalysisResult:
    files = [make_file()] if files is None else files
    suggestions = suggestions or []
    pr = PRSummary(
        number=7, title="Add a helper", author="dev", repo="octo/app", base_branch="main",
        head_branch="feature", total_additions=sum(f.additions for f in files),
        total_deletions=sum(f.deletions for f in files), is_draft=False, files=files,
        head_sha=head_sha,
    )
    critical = sum(1 for s in suggestions if s.severity == "CRITICAL")
    high = sum(1 for s in suggestions if s.severity == "HIGH")
    quality = QualityScore(overall_score=score, grade=grade, passed=passed, summary="",
                           delta=None, critical_count=critical, high_count=high,
                           dimension_scores={"security": 100.0, "style": 90.0})
    return AnalysisResult(pr=pr, suggestions=suggestions, score=quality, duration=1.5,
                          llm_provider_used=llm, warnings=warnings or [])


def make_outcome(result: AnalysisResult | None = None,
                 changed: tuple[ChangedFile, ...] | None | str = "from-result",
                 **fields) -> AnalysisOutcome:
    result = result or make_result()
    if changed == "from-result":
        changed = tuple(ChangedFile(f.filename, f.status, f.additions + f.deletions, True)
                        for f in result.pr.files)
    return AnalysisOutcome(result=result, changed_files=changed, **fields)


class FakeGateway:
    """A GitHub that records every call. `pulls` are returned in turn by pull(), the last one
    again once they run out."""

    def __init__(self, *pulls: PullInfo, merge: MergeResult | Exception | None = None,
                 check_run_error: Exception | None = None,
                 reviewers_error: Exception | None = None, pull_error: Exception | None = None):
        self.pulls = list(pulls) or [make_pull()]
        self.merge_result = merge if merge is not None else MergeResult(True, "Merged",
                                                                       sha="c" * 40)
        self.check_run_error = check_run_error
        self.reviewers_error = reviewers_error
        self.pull_error = pull_error
        self.calls: list[tuple] = []
        self.closed = False

    def pull(self, repo: str, number: int) -> PullInfo:
        self.calls.append(("pull", repo, number))
        if self.pull_error is not None:
            raise self.pull_error
        return self.pulls.pop(0) if len(self.pulls) > 1 else self.pulls[0]

    def create_check_run(self, repo, head_sha, conclusion, title, summary) -> None:
        self.calls.append(("check_run", head_sha, conclusion, title, summary))
        if self.check_run_error is not None:
            raise self.check_run_error

    def merge(self, repo, number, sha, method, title) -> MergeResult:
        self.calls.append(("merge", repo, number, sha, method, title))
        if isinstance(self.merge_result, Exception):
            raise self.merge_result
        return self.merge_result

    def request_reviewers(self, repo, number, users, teams) -> None:
        self.calls.append(("reviewers", users, teams))
        if self.reviewers_error is not None:
            raise self.reviewers_error

    def close(self) -> None:
        self.closed = True

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeAccess:
    def __init__(self, gateway: FakeGateway, checks: bool = True, token: str = "ghs_test"):
        self._gateway = gateway
        self.checks = checks
        self._token = token

    def token(self, installation_id):
        return self._token

    def gateway(self, installation_id):
        return self._gateway


class FakeAnalyzer:
    def __init__(self, outcome: AnalysisOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome or make_outcome()
        self.error = error
        self.calls: list[dict] = []

    def analyze(self, pull, *, token, post_review, skip_llm) -> AnalysisOutcome:
        self.calls.append({"pull": pull, "token": token, "post_review": post_review,
                           "skip_llm": skip_llm})
        if self.error is not None:
            raise self.error
        return self.outcome


def audit_actions(store: Store) -> list[str]:
    with store.read() as conn:
        return [row["action"] for row in conn.execute("SELECT action FROM audit_log ORDER BY id")]
