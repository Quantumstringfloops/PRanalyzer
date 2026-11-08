"""Per-repository policy, from review-app.toml: what the app may do and the thresholds it decides by.

    [defaults]
    merge_min_score = 90

    [repos."owner/name"]
    mode = "auto"          # merge for real; only a repository table may say so

The file belongs to whoever runs the app, not to the repositories, so a PR can't loosen its own
policy. Every problem is an error: a typo must not quietly fall back to a looser default."""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pr_analyzer.analyzer.scorer import GRADE_THRESHOLDS
from pr_analyzer.project_config import glob_regex
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# best first: A+, A, A-, ..., F
GRADES: tuple[str, ...] = tuple(grade for _, grade in GRADE_THRESHOLDS)
REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

Grade = Literal["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F"]
UserLogin = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")]
TeamSlug = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
PathGlob = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class PolicyError(Exception):
    def __init__(self, source: str, problems: list[str]):
        self.problems = problems
        super().__init__(f"{source} is invalid: " + "; ".join(problems))


def grade_rank(grade: str) -> int:
    """Higher is better; -1 for anything that isn't a grade, below F."""
    return len(GRADES) - GRADES.index(grade) if grade in GRADES else -1


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    # recommend: report the decision and do nothing else. auto: merge, request reviewers.
    mode: Literal["recommend", "auto"] = "recommend"
    merge_min_score: float = Field(default=90.0, ge=0, le=100)
    merge_min_grade: Grade = "A-"
    merge_min_confidence: float = Field(default=0.8, ge=0, le=1)
    block_below_score: float = Field(default=65.0, ge=0, le=100)
    block_below_grade: Grade = "D"
    require_llm_review: bool = True
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    # branches PRs may be merged into; empty = the repository's default branch only
    base_branches: tuple[str, ...] = ()
    # a PR that changes any of these always goes to a human (.gitignore-style patterns)
    review_paths: tuple[PathGlob, ...] = (".github/**", ".pr-analyzer.toml", "CODEOWNERS")
    # asked for a review when a PR needs one, in auto mode
    reviewers: tuple[UserLogin, ...] = ()
    team_reviewers: tuple[TeamSlug, ...] = ()
    # post pr-analyzer's review, with its inline comments, on the PR
    post_review: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> "Policy":
        if self.block_below_score > self.merge_min_score:
            raise ValueError("block_below_score can't be above merge_min_score")
        if grade_rank(self.block_below_grade) > grade_rank(self.merge_min_grade):
            raise ValueError("block_below_grade can't be better than merge_min_grade")
        return self

    def allows_base(self, base: str, default_branch: str) -> bool:
        return base in self.base_branches if self.base_branches else base == default_branch

    def review_path_hits(self, paths: list[str]) -> list[str]:
        return [p for p in paths if any(glob_regex(g).fullmatch(p) for g in self.review_paths)]


@dataclass(frozen=True)
class PolicyBook:
    defaults: Policy = field(default_factory=Policy)
    repos: dict[str, Policy] = field(default_factory=dict)   # keyed by lower-case owner/name
    source: str = "built-in defaults"

    def for_repo(self, full_name: str) -> Policy:
        return self.repos.get(full_name.lower(), self.defaults)

    def auto_repos(self) -> list[str]:
        return sorted(name for name, policy in self.repos.items() if policy.mode == "auto")


def load_policy(path: Path) -> PolicyBook:
    """The policy in `path`; the built-in defaults (recommend-only everywhere) if there's no file."""
    if not path.exists():
        return PolicyBook()
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise PolicyError(str(path), [e.strerror or str(e)]) from e
    return parse_policy(raw, source=str(path))


def parse_policy(raw: bytes | str, source: str = "review-app.toml") -> PolicyBook:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PolicyError(source, [f"not valid TOML: {e}"]) from e

    problems = [f"unknown table or key {key!r}: use [defaults] and [repos.\"owner/name\"]"
                for key in data if key not in ("defaults", "repos")]
    defaults_table = data.get("defaults", {})
    repos_table = data.get("repos", {})
    if not isinstance(defaults_table, dict):
        problems.append("[defaults] must be a table")
        defaults_table = {}
    if not isinstance(repos_table, dict):
        problems.append("[repos] must hold one table per repository")
        repos_table = {}
    if defaults_table.get("mode") == "auto":
        problems.append('[defaults] can\'t set mode = "auto": turn auto-merge on per repository')

    defaults = _validate("[defaults]", defaults_table, {}, problems) or Policy()
    repos: dict[str, Policy] = {}
    for name, table in repos_table.items():
        where = f'[repos."{name}"]'
        if not REPO_NAME.fullmatch(name):
            problems.append(f"{where}: not an owner/name repository name")
            continue
        if not isinstance(table, dict):
            problems.append(f"{where} must be a table")
            continue
        if name.lower() in repos:
            problems.append(f"{where} is listed twice (names are matched ignoring case)")
            continue
        policy = _validate(where, table, defaults.model_dump(), problems)
        if policy is not None:
            repos[name.lower()] = policy

    if problems:
        raise PolicyError(source, problems)
    return PolicyBook(defaults=defaults, repos=repos, source=source)


def _validate(where: str, table: dict[str, Any], base: dict[str, Any],
              problems: list[str]) -> Policy | None:
    try:
        return Policy(**{**base, **table})
    except ValidationError as e:
        for error in e.errors():
            location = ".".join(str(part) for part in error["loc"])
            problems.append(f"{where} {location}: {error['msg']}" if location
                            else f"{where}: {error['msg']}")
        return None
