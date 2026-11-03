"""Per-repository settings from a `.pr-analyzer.toml` file.

    disable = ["magic-number", "llm"]       # rule ids and whole stages to switch off
    ignore_paths = ["docs/**", "*.min.js"]  # files to leave out of the analysis
    block_on = ["CRITICAL", "HIGH"]         # severities that block the PR
    inline_min_severity = "MEDIUM"          # lowest severity that gets an inline comment

    [thresholds]
    complexity_medium = 15                  # a function above this is MEDIUM...
    complexity_high = 30                    # ...and above this, HIGH
    max_file_lines = 500
    max_line_length = 120

Every key is optional. When a PR is analysed the file is read from the PR's base branch, never
from the PR itself, so a PR can't loosen its own checks; `--config PATH` names a local file
instead. A missing file means the defaults; an invalid one is an error that names every problem.
"""
import dataclasses
import difflib
import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pr_analyzer.analyzer import ast_analyzer
from pr_analyzer.analyzer.ast_analyzer import Thresholds
from pr_analyzer.exceptions import ConfigError
from pr_analyzer.llm.engine import LLM_RULE
from pr_analyzer.security import scanner

CONFIG_FILE = ".pr-analyzer.toml"

# lowest first, so a severity's position is its rank
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
RULE_IDS = frozenset(ast_analyzer.RULE_IDS + scanner.RULE_IDS + (LLM_RULE,))
KEYS = ("disable", "ignore_paths", "block_on", "inline_min_severity", "thresholds")
THRESHOLD_KEYS = tuple(f.name for f in dataclasses.fields(Thresholds))

# The part of .gitignore's syntax that ignore_paths supports, matched as git matches it: `*` and
# `?` stay within one path segment, and `**` as a whole segment crosses directories (`**/` any
# number of them, a final `/**` everything inside); any other run of asterisks is a `*`, and
# everything else is literal. The syntax in _UNSUPPORTED is rejected instead.
_GLOB_TOKEN = re.compile(
    r"(?P<dirs>(?:\A|(?<=/))\*{2,}/)|(?P<rest>(?:\A|(?<=/))\*{2,}\Z)|(?P<star>\*+)|(?P<one>\?)"
    r"|[^*?]+"
)
_GLOB_PARTS = {"dirs": "(?:.*/)?", "rest": ".*", "star": "[^/]*", "one": "[^/]"}
# syntax that git reads differently from the literal text glob_regex would make of it
_UNSUPPORTED = (
    (re.compile(r"\A!"), "uses negation (a leading !), which isn't supported: ignore_paths can "
                         "only leave files out"),
    (re.compile(r"[\[\]]"), "uses [ or ] (a character class in git), which isn't supported: "
                            "list each name, or use ? for any one character"),
    (re.compile(r"\\"), "uses a backslash, which isn't supported: there are no escapes, and / "
                        "separates directories"),
    # git's own matcher treats foo**/bar inconsistently: across directories or within one
    (re.compile(r"[^/*]\*{2,}/[^/]"), "has ** inside a name before a /, which isn't supported: "
                                      "write foo*/bar for one directory, or foo*/**/bar for any "
                                      "number"),
)


@dataclass(frozen=True)
class ProjectConfig:
    disable: frozenset[str] = frozenset()
    ignore_paths: tuple[str, ...] = ()
    block_on: frozenset[str] = frozenset({"CRITICAL", "HIGH"})
    inline_min_severity: str = "MEDIUM"
    thresholds: Thresholds = Thresholds()

    def enabled(self, rule_id: str) -> bool:
        return rule_id not in self.disable

    def ignores(self, path: str) -> bool:
        """Whether the file matches one of ignore_paths."""
        return any(glob_regex(pattern).fullmatch(path) for pattern in self.ignore_paths)


@lru_cache(maxsize=None)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Match repo paths the way .gitignore does, for the syntax ignore_paths accepts: a pattern
    with no `/` except a trailing one matches at any depth, one with a leading or inner `/` only
    from the repository root, a trailing `/` only directories, and a pattern that matches a
    directory covers every file below it."""
    anchored = "/" in pattern.rstrip("/")
    directory_only = pattern.endswith("/")
    body = "".join(
        _GLOB_PARTS[token.lastgroup] if token.lastgroup else re.escape(token.group())
        for token in _GLOB_TOKEN.finditer(pattern.strip("/"))
    )
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if directory_only else "(?:/.*)?"
    return re.compile(prefix + body + suffix)


def severity_rank(severity: str) -> int:
    """Position in SEVERITIES; -1 for anything unknown, which ranks below every real one."""
    normalized = str(severity).strip().upper()
    return SEVERITIES.index(normalized) if normalized in SEVERITIES else -1


def load_project_config(path: str | Path) -> ProjectConfig:
    """Settings from a local file, as named by --config. Unlike the base-branch lookup, a missing
    file is an error here: someone asked for it by name."""
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise ConfigError(f"Can't read {path}: {e.strerror or e}") from e
    return parse_project_config(raw, source=str(path))


def parse_project_config(raw: bytes | str, source: str = CONFIG_FILE) -> ProjectConfig:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data = tomllib.loads(text)
    except UnicodeDecodeError as e:
        raise ConfigError(f"{source} is not UTF-8 text") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{source} is not valid TOML: {e}") from e

    problems: list[str] = []
    values = _validate(data, problems)
    if problems:
        raise ConfigError(f"{source} is invalid:\n" + "\n".join(f"  - {p}" for p in problems))
    return ProjectConfig(**values)


def _validate(data: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    problems += [unknown("key", key, KEYS) for key in data if key not in KEYS]
    values: dict[str, Any] = {}
    if "disable" in data:
        values["disable"] = frozenset(_rule_ids(data["disable"], problems))
    if "ignore_paths" in data:
        values["ignore_paths"] = tuple(_ignore_paths(data["ignore_paths"], problems))
    if "block_on" in data:
        values["block_on"] = frozenset(_severities("block_on", data["block_on"], problems))
    if "inline_min_severity" in data:
        values["inline_min_severity"] = _severity(
            "inline_min_severity", data["inline_min_severity"], problems
        )
    if "thresholds" in data:
        values["thresholds"] = _thresholds(data["thresholds"], problems)
    return values


def unknown(kind: str, name: str, known: Iterable[str]) -> str:
    """'unknown rule id "magic-numbers" (did you mean "magic-number"?)'"""
    # compare as lower-case kebab-case, so MAGIC_NUMBER and ignore-paths find their match
    by_form = {k.lower().replace("_", "-"): k for k in known}
    close = difflib.get_close_matches(str(name).lower().replace("_", "-"), list(by_form), n=1)
    if close:
        return f"unknown {kind} {name!r} (did you mean {by_form[close[0]]!r}?)"
    return f"unknown {kind} {name!r}; use one of: {', '.join(sorted(by_form.values()))}"


def _strings(key: str, value: Any, problems: list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    problems.append(f'{key} must be a list of strings, e.g. {key} = ["..."]')
    return []


def _rule_ids(value: Any, problems: list[str]) -> list[str]:
    ids = _strings("disable", value, problems)
    problems += [
        f"disable: {unknown('rule id', rule, RULE_IDS)}" for rule in ids if rule not in RULE_IDS
    ]
    return [rule for rule in ids if rule in RULE_IDS]


def _ignore_paths(value: Any, problems: list[str]) -> list[str]:
    patterns = _strings("ignore_paths", value, problems)
    found = {pattern: _pattern_problems(pattern) for pattern in patterns}
    problems += [f"ignore_paths: {p!r} {why}" for p, whys in found.items() for why in whys]
    return [p for p in patterns if not found[p]]


def _pattern_problems(pattern: str) -> list[str]:
    if not pattern.strip("/"):
        return ["is not a path pattern"]
    # rejected rather than read literally: each would quietly match something other than what
    # the same line does in a .gitignore
    return [why for syntax, why in _UNSUPPORTED if syntax.search(pattern)]


def _severity(key: str, value: Any, problems: list[str]) -> str:
    if isinstance(value, str) and severity_rank(value) >= 0:
        return value.strip().upper()
    problems.append(f"{key}: {value!r} is not a severity; use one of {', '.join(SEVERITIES)}")
    return ""


def _severities(key: str, value: Any, problems: list[str]) -> list[str]:
    severities = (_severity(key, item, problems) for item in _strings(key, value, problems))
    return [s for s in severities if s]


def _thresholds(value: Any, problems: list[str]) -> Thresholds:
    if not isinstance(value, dict):
        problems.append("thresholds must be a table: a [thresholds] section")
        return Thresholds()
    limits: dict[str, int] = {}
    for key, number in value.items():
        if key not in THRESHOLD_KEYS:
            problems.append(f"thresholds: {unknown('key', key, THRESHOLD_KEYS)}")
        elif isinstance(number, bool) or not isinstance(number, int) or number < 1:
            # shown the way TOML writes it: true, "120", 99.5
            shown = json.dumps(number, default=str)
            problems.append(f"thresholds.{key} must be a whole number above 0, not {shown}")
        else:
            limits[key] = number

    thresholds = dataclasses.replace(Thresholds(), **limits)
    if thresholds.complexity_medium > thresholds.complexity_high:
        problems.append(
            f"thresholds.complexity_medium ({thresholds.complexity_medium}) must not be above "
            f"complexity_high ({thresholds.complexity_high})"
        )
    return thresholds
