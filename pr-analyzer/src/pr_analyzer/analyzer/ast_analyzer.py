import os
import re
from bisect import bisect_left
from collections.abc import Iterator
from functools import lru_cache

import structlog
import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

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
COMMENT_PREFIXES = ("#", "//", "/*", "*")

# Each rule runs on one added line at a time (the text after the leading "+").
# Hardcoded secrets are not checked here: the security scanner owns them, and checking them in
# both places used to penalise the same line twice.
PATTERNS = [
    {
        "id": "TODO",
        "regex": re.compile(r"\b(TODO|FIXME)\b"),
        "extensions": CODE_EXTENSIONS,
        "skip_comments": False,
        "severity": "LOW",
        "category": "style",
        "title": "TODO/FIXME comment added",
        "description": "This line introduces a TODO or FIXME marker. Consider "
                        "tracking this in an issue tracker instead of leaving "
                        "it in code.",
    },
    {
        "id": "CONSOLE_LOG",
        "regex": re.compile(r"\bconsole\.log\s*\("),
        "extensions": JS_TS,
        "skip_comments": True,
        "severity": "LOW",
        "category": "style",
        "title": "console.log left in code",
        "description": "Debug logging via console.log was added. Consider "
                        "removing it or replacing it with a proper logger "
                        "before merging.",
    },
    {
        "id": "PRINT_STATEMENT",
        "regex": re.compile(r"(?<![\w.])print\s*\("),
        "extensions": PYTHON,
        "skip_comments": True,
        "severity": "LOW",
        "category": "style",
        "title": "print() left in code",
        "description": "A print() call was added. In production code this "
                        "is usually better handled through structured "
                        "logging.",
    },
    {
        "id": "BARE_EXCEPT",
        "regex": re.compile(r"^\s*except\s*:"),
        "extensions": PYTHON,
        "skip_comments": True,
        "severity": "MEDIUM",
        "category": "security",
        "title": "Bare except clause",
        "description": "A bare 'except:' catches every exception, including "
                        "ones you don't expect (KeyboardInterrupt, "
                        "SystemExit). Catch a specific exception type "
                        "instead.",
    },
    {
        "id": "LONG_LINE",
        "regex": re.compile(r"^.{121,}$"),
        "extensions": CODE_EXTENSIONS,
        "skip_comments": False,
        "severity": "LOW",
        "category": "style",
        "title": "Line exceeds 120 characters",
        "description": "This added line is unusually long, which can hurt "
                        "readability. Consider breaking it up.",
    },
    {
        "id": "MAGIC_NUMBER",
        "regex": re.compile(r"[^\w.](?<!0x)\b(?!0\b|1\b)\d{2,}\b"),
        "extensions": CODE_EXTENSIONS,
        "skip_comments": True,
        "skip_tests": True,
        # defining a named constant is the fix this rule asks for, so don't flag it
        "skip_if": re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*(:[^=]*)?="),
        "severity": "LOW",
        "category": "style",
        "title": "Possible magic number",
        "description": "A numeric literal was added without an obvious "
                        "named constant. If this number has meaning, "
                        "consider naming it.",
    },
    {
        "id": "MISSING_TYPE_HINT",
        "regex": re.compile(r"^\s*(async\s+)?def\s+\w+\s*\([^)]*\)\s*:"),
        "extensions": PYTHON,
        "skip_comments": True,
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


def file_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def is_comment(text: str) -> bool:
    return text.lstrip().startswith(COMMENT_PREFIXES)


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


class StaticAnalyzer:

    def analyze(self, files: list[FileChange]) -> list[Suggestion]:
        suggestions = []
        for file in files:
            if file.patch is None:
                continue
            if file.status == "removed":
                continue
            suggestions.extend(self.scan_patterns(file))
            suggestions.extend(self.scan_tree_sitter(file))

        log.info("static analysis complete", total_suggestions=len(suggestions))
        return suggestions

    def scan_patterns(self, file: FileChange) -> list[Suggestion]:
        if file.patch is None:
            return []
        extension = file_extension(file.filename)
        test_file = is_test_file(file.filename)

        suggestions = []
        for line_no, text in iter_added_lines(file.patch):
            comment = is_comment(text)
            for pattern in PATTERNS:
                if extension not in pattern["extensions"]:
                    continue
                if comment and pattern["skip_comments"]:
                    continue
                if test_file and pattern.get("skip_tests"):
                    continue
                skip_if = pattern.get("skip_if")
                if skip_if is not None and skip_if.search(text):
                    continue
                if pattern["regex"].search(text):
                    suggestions.append(Suggestion(
                        filename=file.filename,
                        severity=pattern["severity"],
                        category=pattern["category"],
                        title=pattern["title"],
                        description=pattern["description"],
                        line=line_no,
                        source="static",
                    ))
        return suggestions

    def scan_tree_sitter(self, file: FileChange) -> list[Suggestion]:
        if file.raw_content is None:
            return []
        language = language_name(file.filename)
        if language is None:
            return []

        suggestions = []
        try:
            tree = Parser(_load_language(language)).parse(file.raw_content.encode("utf-8"))
            suggestions.extend(self._complex_functions(file, tree.root_node, language))
        except Exception as e:  # a parser problem must not stop the rest of the analysis
            log.warning("tree_sitter_failed", filename=file.filename, error=str(e))

        lines = len(file.raw_content.splitlines())
        if lines > MAX_FILE_LINES:
            suggestions.append(Suggestion(
                filename=file.filename,
                severity="LOW",
                category="complexity",
                title="File too long",
                description=f"This file has {lines} lines. Consider splitting it into smaller modules.",
                line=None,
                source="static",
            ))

        return suggestions

    def _complex_functions(self, file: FileChange, root: Node, language: str) -> list[Suggestion]:
        # only report functions this PR touches; untouched legacy code isn't the PR's fault
        added = sorted(line_no for line_no, _ in iter_added_lines(file.patch or ""))
        suggestions = []
        for function in iter_functions(root, language):
            start, end = function.start_point[0] + 1, function.end_point[0] + 1
            i = bisect_left(added, start)
            if i == len(added) or added[i] > end:
                continue

            complexity = 1 + count_complexity(function, language)
            if complexity > COMPLEXITY_HIGH:
                severity, title, limit = "HIGH", "Very high cyclomatic complexity", COMPLEXITY_HIGH
            elif complexity > COMPLEXITY_MEDIUM:
                severity, title, limit = "MEDIUM", "High cyclomatic complexity", COMPLEXITY_MEDIUM
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
            ))
        return suggestions
