import os
import re
from bisect import bisect_left
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from typing import Any

import structlog
import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from pr_analyzer.analyzer.line_lexer import LineParts, split_line
from pr_analyzer.diff import iter_added_lines
from pr_analyzer.models import FileChange, Suggestion

log = structlog.get_logger(__name__)


# for the treesitter

LANGUAGE_MAP = {
    ".py":  "python",
    ".js":  "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts":  "typescript",
    ".tsx": "tsx",
}

PYTHON = {".py"}
JS_TS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
# style rules only make sense in source code, not in docs, lockfiles or data files
CODE_EXTENSIONS = PYTHON | JS_TS | {
    ".java", ".go", ".rb", ".php", ".cs", ".rs", ".kt", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".scala", ".sh",
}

# numbers whose meaning is plain wherever they appear
WELL_KNOWN_NUMBERS = frozenset({
    10, 100, 1000,                                  # powers of ten: percentages, scaling, ms
    24, 60, 3600, 86400,                            # hours in a day; seconds in a minute/hour/day
    16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 65536,   # bit widths and buffer sizes
    255, 65535,                                     # the largest 8- and 16-bit values
    80, 443,                                        # the HTTP and HTTPS ports
}) | {int(status) for status in HTTPStatus}        # standard HTTP status codes, 100 to 511
# `timeout=30`, `retries = 5`, `"port": 8080`, `{ port: 8080 }`: the name right before the number
# says what it means. Comparisons (==, <=, ...) and augmented assignments (+=) don't name it.
NAMED_BY = re.compile(r"(?:\b[A-Za-z_]\w*|[\"'\]])\s*(?::|(?<![=!<>+\-*/%&|^~:@])=)\s*[-+]?$")


def in_subscript(code: str, pos: int) -> bool:
    """Whether `pos` is inside [...] that follows a name, a call or a string: an index or a slice
    such as `line[:72]` or `parts[-2]`, not a list literal."""
    opening = code.rfind("[", 0, pos)
    if opening < 1 or code.rfind("]", 0, pos) > opening:
        return False
    before = code[opening - 1]
    return before.isalnum() or before in "_)]\"'"


def explained_number(code: str, match: re.Match[str]) -> bool:
    """A number a reader understands without a named constant."""
    return (
        int(match.group()) in WELL_KNOWN_NUMBERS
        or NAMED_BY.search(code, 0, match.start()) is not None
        or in_subscript(code, match.start())
    )


# Each rule runs on one added line at a time (the text after the leading "+"). Its "id" is the
# stable name .pr-analyzer.toml uses to switch it off. Its "scope" picks the view of the line it
# reads (see line_lexer): "code" leaves out comments and the contents of strings, "comments" is
# the comments alone. An optional "exempt" callable can wave off individual matches.
# Hardcoded secrets are not checked here: the security scanner owns them, and checking them in
# both places used to penalise the same line twice.
PATTERNS: list[dict[str, Any]] = [
    {
        "id": "todo-comment",
        "regex": re.compile(r"\b(TODO|FIXME)\b"),
        "extensions": CODE_EXTENSIONS,
        # in a string the marker is data, such as a status value, not a note to the team
        "scope": "comments",
        "severity": "LOW",
        "category": "style",
        "title": "TODO/FIXME comment added",
        "description": "This line introduces a TODO or FIXME marker. Consider "
                        "tracking this in an issue tracker instead of leaving "
                        "it in code.",
    },
    {
        "id": "console-log",
        "regex": re.compile(r"\bconsole\.log\s*\("),
        "extensions": JS_TS,
        "scope": "code",
        "severity": "LOW",
        "category": "style",
        "title": "console.log left in code",
        "description": "Debug logging via console.log was added. Consider "
                        "removing it or replacing it with a proper logger "
                        "before merging.",
    },
    {
        "id": "print-call",
        "regex": re.compile(r"(?<![\w.])print\s*\("),
        "extensions": PYTHON,
        "scope": "code",
        "severity": "LOW",
        "category": "style",
        "title": "print() left in code",
        "description": "A print() call was added. In production code this "
                        "is usually better handled through structured "
                        "logging.",
    },
    {
        "id": "bare-except",
        "regex": re.compile(r"^\s*except\s*:"),
        "extensions": PYTHON,
        "scope": "code",
        "severity": "MEDIUM",
        "category": "security",
        "title": "Bare except clause",
        "description": "A bare 'except:' catches every exception, including "
                        "ones you don't expect (KeyboardInterrupt, "
                        "SystemExit). Catch a specific exception type "
                        "instead.",
    },
    {
        "id": "magic-number",
        # an integer of two or more digits, not part of a name or a decimal fraction
        "regex": re.compile(r"(?<=[^\w.])\d{2,}\b"),
        "extensions": CODE_EXTENSIONS,
        "scope": "code",
        "skip_tests": True,
        # defining a named constant (_PRIVATE ones too) is the fix this rule asks for, and a
        # version check such as `sys.version_info >= (3, 11)` names its own numbers
        "skip_if": re.compile(r"^\s*_*[A-Z][A-Z0-9_]*\s*(:[^=]*)?=|(?i:(?:\b|_)version)"),
        "exempt": explained_number,
        "severity": "LOW",
        "category": "style",
        "title": "Possible magic number",
        "description": "A numeric literal was added without an obvious "
                        "named constant. If this number has meaning, "
                        "consider naming it.",
    },
    {
        "id": "missing-return-type",
        "regex": re.compile(r"^\s*(async\s+)?def\s+\w+\s*\([^)]*\)\s*:"),
        "extensions": PYTHON,
        "scope": "code",
        # test functions rarely annotate a return type; flagging each one buried real findings
        "skip_tests": True,
        "severity": "LOW",
        "category": "style",
        "title": "Function missing return type hint",
        "description": "This function definition has no '->' return type "
                        "annotation. Note: this pattern alone can't check "
                        "parameter hints, and signatures that wrap over "
                        "several lines are not checked.",
    },
]

# McCabe complexity = 1 + the number of decision points in a function
FUNCTION_NODES = {
    "python": {"function_definition"},
    "javascript": {
        "function_declaration", "function_expression", "arrow_function",
        "method_definition", "generator_function_declaration", "generator_function",
    },
}
DECISION_NODES = {
    "python": {
        "if_statement", "elif_clause", "for_statement", "while_statement", "except_clause",
        "case_clause", "conditional_expression", "boolean_operator", "for_in_clause", "if_clause",
    },
    "javascript": {
        "if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement",
        "switch_case", "catch_clause", "ternary_expression",
    },
}
for _name in ("typescript", "tsx"):
    FUNCTION_NODES[_name] = FUNCTION_NODES["javascript"]
    DECISION_NODES[_name] = DECISION_NODES["javascript"]
# in JS/TS, && || ?? are binary_expression nodes, told apart by their operator
LOGICAL_OPERATORS = {"&&", "||", "??"}

COMPLEXITY_MEDIUM = 15
COMPLEXITY_HIGH = 30
MAX_FILE_LINES = 500
MAX_LINE_LENGTH = 120

# the checks that compare against a threshold rather than match a pattern
LONG_LINE_RULE = "long-line"
COMPLEXITY_RULE = "complexity"
FILE_LENGTH_RULE = "file-length"
RULE_IDS = tuple(p["id"] for p in PATTERNS) + (LONG_LINE_RULE, COMPLEXITY_RULE, FILE_LENGTH_RULE)


@dataclass(frozen=True)
class Thresholds:
    """Limits the static checks compare against; .pr-analyzer.toml can change each one."""
    complexity_medium: int = COMPLEXITY_MEDIUM   # a function above this is MEDIUM...
    complexity_high: int = COMPLEXITY_HIGH       # ...and above this, HIGH
    max_file_lines: int = MAX_FILE_LINES
    max_line_length: int = MAX_LINE_LENGTH


def file_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def is_test_file(filename: str) -> bool:
    parts = filename.replace("\\", "/").lower().split("/")
    name = parts[-1]
    return (
        any(part in ("test", "tests", "__tests__", "spec") for part in parts[:-1])
        or name.startswith("test_")
        or re.search(r"(_test\.py|\.(test|spec)\.[cm]?[jt]sx?)$", name) is not None
    )


def language_name(filename: str) -> str | None:
    return LANGUAGE_MAP.get(file_extension(filename))


@lru_cache(maxsize=None)
def _load_language(name: str) -> Language:
    if name == "python":
        return Language(tspython.language())
    if name == "javascript":
        return Language(tsjavascript.language())
    if name == "typescript":
        return Language(tstypescript.language_typescript())
    if name == "tsx":
        return Language(tstypescript.language_tsx())
    raise ValueError(f"no tree-sitter grammar for {name!r}")


def get_language(filename: str) -> Language | None:
    name = language_name(filename)
    return _load_language(name) if name else None


def count_complexity(node: Node, language: str = "python") -> int:
    """Count decision points under `node`. Nested functions are skipped: they are measured
    on their own. Walks iteratively, so deeply nested code can't hit the recursion limit."""
    decisions = DECISION_NODES[language]
    functions = FUNCTION_NODES[language]
    count = 0
    stack = list(node.children)
    while stack:
        current = stack.pop()
        if current.type in functions:
            continue
        if current.type in decisions:
            count += 1
        elif current.type == "binary_expression":
            operator = current.child_by_field_name("operator")
            if operator is not None and operator.type in LOGICAL_OPERATORS:
                count += 1
        stack.extend(current.children)
    return count


def iter_functions(root: Node, language: str) -> Iterator[Node]:
    functions = FUNCTION_NODES[language]
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in functions:
            yield node
        stack.extend(node.children)


def function_name(node: Node) -> str:
    name = node.child_by_field_name("name")
    if name is None and node.parent is not None and node.parent.type == "variable_declarator":
        name = node.parent.child_by_field_name("name")   # const handler = () => {...}
    if name is None or name.text is None:
        return "<anonymous>"
    return name.text.decode("utf-8", errors="replace")


def patterns_for(filename: str, disabled: Collection[str] = ()) -> list[dict[str, Any]]:
    """The line rules that apply to this file and aren't switched off."""
    extension = file_extension(filename)
    test_file = is_test_file(filename)
    return [
        p for p in PATTERNS
        if p["id"] not in disabled
        and extension in p["extensions"]
        and not (test_file and p.get("skip_tests"))
    ]


def line_matches(pattern: dict[str, Any], parts: LineParts) -> bool:
    text = parts.comments if pattern["scope"] == "comments" else parts.code
    skip_if = pattern.get("skip_if")
    if skip_if is not None and skip_if.search(text):
        return False
    exempt = pattern.get("exempt")
    return any(exempt is None or not exempt(text, m) for m in pattern["regex"].finditer(text))


def pattern_finding(file: FileChange, pattern: dict[str, Any], line_no: int) -> Suggestion:
    return Suggestion(
        filename=file.filename,
        severity=pattern["severity"],
        category=pattern["category"],
        title=pattern["title"],
        description=pattern["description"],
        line=line_no,
        source="static",
        rule_id=pattern["id"],
    )


def long_line_finding(file: FileChange, line_no: int, limit: int) -> Suggestion:
    return Suggestion(
        filename=file.filename,
        severity="LOW",
        category="style",
        title=f"Line exceeds {limit} characters",
        description="This added line is unusually long, which can hurt readability. "
                    "Consider breaking it up.",
        line=line_no,
        source="static",
        rule_id=LONG_LINE_RULE,
    )


class StaticAnalyzer:

    def analyze(
        self,
        files: list[FileChange],
        disabled: Collection[str] = (),
        thresholds: Thresholds = Thresholds(),
    ) -> list[Suggestion]:
        """Run every static check on the files. `disabled` holds rule ids (see RULE_IDS) to skip."""
        suggestions = []
        for file in files:
            if file.patch is None or file.status == "removed":
                continue
            suggestions.extend(self.scan_patterns(file, disabled, thresholds.max_line_length))
            suggestions.extend(self.scan_tree_sitter(file, disabled, thresholds))

        log.info("static analysis complete", total_suggestions=len(suggestions))
        return suggestions

    def scan_patterns(
        self, file: FileChange, disabled: Collection[str] = (), max_line_length: int = MAX_LINE_LENGTH
    ) -> list[Suggestion]:
        if file.patch is None:
            return []
        extension = file_extension(file.filename)
        patterns = patterns_for(file.filename, disabled)
        check_length = LONG_LINE_RULE not in disabled and extension in CODE_EXTENSIONS

        suggestions: list[Suggestion] = []
        for line_no, text in iter_added_lines(file.patch):
            parts = split_line(text, extension)
            suggestions.extend(
                pattern_finding(file, p, line_no) for p in patterns if line_matches(p, parts)
            )
            if check_length and len(text) > max_line_length:
                suggestions.append(long_line_finding(file, line_no, max_line_length))
        return suggestions

    def scan_tree_sitter(
        self, file: FileChange, disabled: Collection[str] = (), thresholds: Thresholds = Thresholds()
    ) -> list[Suggestion]:
        if file.raw_content is None:
            return []
        language = language_name(file.filename)
        if language is None:
            return []

        suggestions = []
        if COMPLEXITY_RULE not in disabled:
            try:
                tree = Parser(_load_language(language)).parse(file.raw_content.encode("utf-8"))
                suggestions.extend(
                    self._complex_functions(file, tree.root_node, language, thresholds)
                )
            except Exception as e:  # a parser problem must not stop the rest of the analysis
                log.warning("tree_sitter_failed", filename=file.filename, error=str(e))

        lines = len(file.raw_content.splitlines())
        if FILE_LENGTH_RULE not in disabled and lines > thresholds.max_file_lines:
            suggestions.append(Suggestion(
                filename=file.filename,
                severity="LOW",
                category="complexity",
                title="File too long",
                description=f"This file has {lines} lines. Consider splitting it into smaller modules.",
                line=None,
                source="static",
                rule_id=FILE_LENGTH_RULE,
            ))

        return suggestions

    def _complex_functions(
        self, file: FileChange, root: Node, language: str, thresholds: Thresholds
    ) -> list[Suggestion]:
        # only report functions this PR touches; untouched legacy code isn't the PR's fault
        added = sorted(line_no for line_no, _ in iter_added_lines(file.patch or ""))
        high, medium = thresholds.complexity_high, thresholds.complexity_medium
        suggestions = []
        for function in iter_functions(root, language):
            start, end = function.start_point[0] + 1, function.end_point[0] + 1
            i = bisect_left(added, start)
            if i == len(added) or added[i] > end:
                continue

            complexity = 1 + count_complexity(function, language)
            if complexity > high:
                severity, title, limit = "HIGH", "Very high cyclomatic complexity", high
            elif complexity > medium:
                severity, title, limit = "MEDIUM", "High cyclomatic complexity", medium
            else:
                continue

            suggestions.append(Suggestion(
                filename=file.filename,
                severity=severity,
                category="complexity",
                title=title,
                description=f"`{function_name(function)}` has a cyclomatic complexity of "
                            f"{complexity} (above {limit}). Consider breaking it into "
                            f"smaller functions.",
                line=start,
                source="static",
                rule_id=COMPLEXITY_RULE,
            ))
        return suggestions
