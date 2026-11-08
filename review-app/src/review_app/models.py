"""Facts the app passes between its parts."""
from dataclasses import dataclass

from pr_analyzer.models import AnalysisResult


@dataclass(frozen=True)
class PullInfo:
    repo: str                     # owner/name
    number: int
    title: str
    author: str
    head_sha: str
    base_branch: str
    default_branch: str
    head_repo_id: int | None      # None when the head repository is gone (a deleted fork)
    base_repo_id: int | None
    is_draft: bool
    state: str                    # "open" | "closed" | "merged"
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0

    @property
    def is_fork(self) -> bool:
        # an unknown head counts as a fork: merging needs proof the branch is the repository's own
        return (self.head_repo_id is None or self.base_repo_id is None
                or self.head_repo_id != self.base_repo_id)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


@dataclass(frozen=True)
class ChangedFile:
    """A file as GitHub lists it in the PR, whether or not it came with a diff."""
    filename: str
    status: str                   # "added" | "modified" | "removed" | "renamed" | ...
    changes: int
    has_patch: bool
    previous_filename: str | None = None


@dataclass
class AnalysisOutcome:
    result: AnalysisResult
    # every file GitHub lists for the PR; None when they couldn't be listed
    changed_files: tuple[ChangedFile, ...] | None = ()
    # pr-analyzer stages (semgrep, dependency-audit, llm) the project switched off
    disabled_stages: tuple[str, ...] = ()
    # scanners not installed where the app runs
    missing_tools: tuple[str, ...] = ()
    # the PR's head moved on while the analysis ran
    head_moved: bool = False


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    message: str
    checks_pending: bool = False   # refused only until required checks pass
    sha: str | None = None         # the merge commit
