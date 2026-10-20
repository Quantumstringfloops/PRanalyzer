import re
from tree_sitter import Language, Parser
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
import tree_sitter_javascript as tsjavascript
from pr_analyzer.models import FileChange, Suggestion




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

def extract_line_numbers(patch: str, match_position:int) -> int|None:
    hunk_pattern = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    current_line = None
    current_hunk_start_pos = None

    for hunk_match in hunk_pattern.finditer(patch):
        if hunk_match.start() <= match_position:
            current_line = int(hunk_match.group(1))
            current_hunk_start_pos = hunk_match.end()
        else:
            break

    if current_line is None:
        return None

    chunk = patch[current_hunk_start_pos:match_position]

    for line in chunk.splitlines():
        if line.startswith("-"):
            continue          # removed line — doesn't exist in new file
        current_line += 1     # added (+) and context lines both count

    return current_line

def get_language(filename:str):
    path_parts = filename.strip('/').split('/')
    file = path_parts[-1].file.split(".").strip(".")
    file_type = file[-1]
    language = LANGUAGE_MAP.get(file_type)

    if language is None:
        return None
    if language == "python":
        return Language(tspython.language())
    if language == "javascript":
        return Language(tsjavascript.language())
    if language == "tsx":
        return Language(tstypescript.language_tsx())
    if language == "typescript":
        return Language(tstypescript.language_typescript())


def count_complexity(node) -> int:
    # count decision nodes if elif else where except case for
    count = 0
    NAMED_NODES =["if_statement", "for_statement", "while_statement", "with_statement", "except_clause", "cause_clause"]

    if node.type in NAMED_NODES:
        count += 1
    for child in node.children:
        count += count_complexity(child)

    return count




    