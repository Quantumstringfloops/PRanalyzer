import re
from tree_sitter import Language, Parser
import tree_sitter_python as tspython
from pr_analyzer.models import FileChange, Suggestion

PYTHON_LANGUAGE = Language(tspython.language())
parser = Parser(PYTHON_LANGUAGE)

# for the treesitter

LANGUAGE_MAP = {
    ".py":  "python",
    ".js":  "javascript",
    ".ts":  "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
}

PATTERNS = [
    {
        "id": "TODO",
        "regex": re.compile(r"^\+(?!\+\+).*\b(TODO|FIXME)\b", re.MULTILINE),
        "severity": "LOW",
        "category": "style",
        "title": "TODO/FIXME comment added",
        "description": "This line introduces a TODO or FIXME marker. Consider "
                        "tracking this in an issue tracker instead of leaving "
                        "it in code.",
    },
    {
        "id": "CONSOLE_LOG",
        "regex": re.compile(r"^\+(?!\+\+).*\bconsole\.log\s*\(", re.MULTILINE),
        "severity": "LOW",
        "category": "style",
        "title": "console.log left in code",
        "description": "Debug logging via console.log was added. Consider "
                        "removing it or replacing it with a proper logger "
                        "before merging.",
    },
    {
        "id": "PRINT_STATEMENT",
        "regex": re.compile(r"^\+(?!\+\+).*\bprint\s*\(", re.MULTILINE),
        "severity": "LOW",
        "category": "style",
        "title": "print() left in code",
        "description": "A print() call was added. In production code this "
                        "is usually better handled through structured "
                        "logging.",
    },
    {
        "id": "BARE_EXCEPT",
        "regex": re.compile(r"^\+(?!\+\+)\s*except\s*:\s*$", re.MULTILINE),
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
        "regex": re.compile(r"^\+(?!\+\+).{121,}$", re.MULTILINE),
        "severity": "LOW",
        "category": "style",
        "title": "Line exceeds 120 characters",
        "description": "This added line is unusually long, which can hurt "
                        "readability. Consider breaking it up.",
    },
    {
        "id": "HARDCODED_SECRET",
        "regex": re.compile(
            r"^\+(?!\+\+).*\b(password|passwd|secret|api[_-]?key)\s*=\s*"
            r"['\"][^'\"]{3,}['\"]",
            re.MULTILINE | re.IGNORECASE,
        ),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Possible hardcoded credential",
        "description": "This line assigns what looks like a secret "
                        "(password/API key) directly as a string literal. "
                        "Secrets should come from environment variables or "
                        "a secrets manager, never committed to source.",
    },
    {
        "id": "MAGIC_NUMBER",
        "regex": re.compile(
            r"^\+(?!\+\+).*[^\w.](?<!0x)\b(?!0\b|1\b)\d{2,}\b",
            re.MULTILINE,
        ),
        "severity": "LOW",
        "category": "style",
        "title": "Possible magic number",
        "description": "A numeric literal was added without an obvious "
                        "named constant. If this number has meaning, "
                        "consider naming it.",
    },
    {
        "id": "MISSING_TYPE_HINT",
        "regex": re.compile(
            r"^\+(?!\+\+)\s*def\s+\w+\([^)]*\)\s*:",
            re.MULTILINE,
        ),
        "severity": "LOW",
        "category": "style",
        "title": "Function missing return type hint",
        "description": "This function definition has no '->' return type "
                        "annotation. Note: this pattern alone can't check "
                        "parameter hints or confirm '->' isn't present "
                        "elsewhere on a wrapped signature — treat it as a "
                        "rough signal, not a guarantee.",
    },
]
