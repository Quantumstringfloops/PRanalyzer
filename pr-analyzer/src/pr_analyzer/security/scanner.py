import re
from collections import Counter
from collections.abc import Collection
from typing import Any

import structlog

from pr_analyzer.analyzer.ast_analyzer import CODE_EXTENSIONS, file_extension
from pr_analyzer.analyzer.line_lexer import split_line
from pr_analyzer.diff import iter_added_lines
from pr_analyzer.models import FileChange, Suggestion
from pr_analyzer.security.dep_auditor import DEPENDENCY_AUDIT_RULE, DepAuditScanner
from pr_analyzer.security.semgrep_runner import SEMGREP_RULE, SemgrepRunner

log = structlog.get_logger(__name__)

# Every pattern runs on one added line at a time, so a match can't span lines and its line
# number is exact. Secrets are checked in every file, comments included. Each "id" is the stable
# name .pr-analyzer.toml uses to switch the pattern off.
SECRET_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "aws-access-key",
        "regex": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "severity": "CRITICAL",
        "category": "security",
        "title": "AWS access key exposed",
        "description": "An AWS access key was found hardcoded. Revoke it immediately and use environment variables.",
    },
    {
        "id": "hardcoded-api-key",
        "regex": re.compile(r"(?i)(api[_-]?key|apikey|api[_-]?secret)\s*[:=]\s*[\"']([A-Za-z0-9_\-]{16,})[\"']"),
        "value_group": 2,
        "severity": "CRITICAL",
        "category": "security",
        "title": "Hardcoded API key",
        "description": "An API key was found hardcoded. Move it to environment variables.",
    },
    {
        "id": "private-key",
        "regex": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Private key exposed",
        "description": "A private PEM key block was found. Remove it immediately and rotate the key.",
    },
    {
        "id": "github-token",
        "regex": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b"),
        "severity": "CRITICAL",
        "category": "security",
        "title": "GitHub personal access token exposed",
        "description": "A GitHub token was found hardcoded. Revoke it immediately at github.com/settings/tokens.",
    },
    {
        "id": "jwt-token",
        "regex": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "severity": "HIGH",
        "category": "security",
        "title": "JWT token hardcoded",
        "description": "A JWT token was found hardcoded. Tokens should never be committed to source control.",
    },
    {
        "id": "hardcoded-password",
        "regex": re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*[\"']([^\"']{4,})[\"']"),
        "value_group": 1,
        "severity": "CRITICAL",
        "category": "security",
        "title": "Hardcoded password",
        "description": "A password was found hardcoded. Move it to environment variables.",
    },
    {
        "id": "db-connection-string",
        "regex": re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?)://"
            r"[^\s:/@\"']+:[^\s@/\"']+@"
        ),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Database connection string with credentials",
        "description": "A database URL with embedded credentials was found. Use environment variables instead.",
    },
]

# Checked only in source files, and only on code: a call named in a comment or written inside a
# string literal isn't a call. String quotes stay visible (see line_lexer), so the SQL rule still
# sees a string being formatted into a query.
DANGEROUS_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "sql-injection",
        # an f-string, or a string literal followed by %, .format( or + ; parameterised calls such
        # as execute("... %s", (value,)) are the safe form and are not flagged
        "regex": re.compile(
            r"\bexecute(?:many)?\s*\(\s*"
            r"(?:[rRbBuU]?[fF][rR]?[\"']|[\"'][^\"']*[\"']\s*(?:%|\.format\s*\(|\+))"
        ),
        "severity": "HIGH",
        "category": "security",
        "title": "Possible SQL injection",
        "description": "SQL query built with string formatting. Use parameterised queries instead.",
    },
    {
        "id": "eval",
        "regex": re.compile(r"(?<![\w.])eval\s*\("),   # not model.eval() or my_eval()
        "severity": "HIGH",
        "category": "security",
        "title": "eval() usage detected",
        "description": "eval() executes arbitrary code. Never use it on user input.",
    },
    {
        "id": "pickle-load",
        "regex": re.compile(r"\bpickle\.loads?\s*\("),
        "severity": "HIGH",
        "category": "security",
        "title": "pickle.load() usage detected",
        "description": "pickle.load() can execute arbitrary code when loading untrusted data. Use JSON instead.",
    },
    {
        "id": "yaml-load",
        "regex": re.compile(
            r"\byaml\.(?:unsafe_load\s*\(|load\s*\((?![^)]*Loader\s*=\s*(?:yaml\.)?C?SafeLoader\b))"
        ),
        "severity": "HIGH",
        "category": "security",
        "title": "yaml.load() without SafeLoader",
        "description": "yaml.load() is unsafe. Use yaml.safe_load() or pass Loader=yaml.SafeLoader.",
    },
    {
        "id": "subprocess-shell",
        "regex": re.compile(r"\bsubprocess\.(?:run|call|check_call|check_output|Popen)\s*\(.*\bshell\s*=\s*True"),
        "severity": "HIGH",
        "category": "security",
        "title": "subprocess with shell=True",
        "description": "shell=True with user input allows command injection. Pass a list of arguments instead.",
    },
    {
        "id": "insecure-random",
        # only when the value looks security-related; random is fine for jitter or shuffling
        "regex": re.compile(
            r"(?i)\b\w*(?:token|passw|secret|otp|nonce|salt|session|api_?key)\w*\s*=.*"
            r"\brandom\.(?:random|randint|choice|choices|randrange|getrandbits)\s*\("
        ),
        "severity": "MEDIUM",
        "category": "security",
        "title": "Non-cryptographic random used",
        "description": "random module is not cryptographically secure. Use secrets module for tokens and passwords.",
    },
    {
        "id": "debug-mode",
        "regex": re.compile(r"(?i)\bdebug\s*=\s*True\b"),
        "severity": "MEDIUM",
        "category": "security",
        "title": "Debug mode enabled",
        "description": "debug=True should never be committed. It exposes stack traces and sensitive data in production.",
    },
]

# obvious placeholders in examples and docs are not leaked secrets
PLACEHOLDER = re.compile(
    r"(?i)^(?:x+|\*+|\.+|<[^>]*>|\$\{[^}]*\}|\{\{?[^}]*\}\}?|%\([^)]*\)s|"
    r".*(?:changeme|change_me|example|placeholder|dummy|redacted|"
    r"your[_-]?(?:api[_-]?)?(?:key|token|password|secret)).*)$"
)

RULE_IDS = tuple(p["id"] for p in SECRET_PATTERNS + DANGEROUS_PATTERNS) + (
    SEMGREP_RULE, DEPENDENCY_AUDIT_RULE,
)


def deduplicate(suggestions: list[Suggestion]) -> list[Suggestion]:
    seen = set()
    unique = []
    for s in suggestions:
        key = (s.filename, s.line, s.title, s.description)
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


class SecurityScanner:

    def __init__(self, semgrep: SemgrepRunner | None = None, dep_auditor: DepAuditScanner | None = None):
        self.semgrep = semgrep or SemgrepRunner()
        self.dep_auditor = dep_auditor or DepAuditScanner()
        self.warnings: list[str] = []

    def scan(self, files: list[FileChange], disabled: Collection[str] = ()) -> list[Suggestion]:
        """Check the files. `disabled` holds rule ids (see RULE_IDS) to skip."""
        secrets = [p for p in SECRET_PATTERNS if p["id"] not in disabled]
        dangerous = [p for p in DANGEROUS_PATTERNS if p["id"] not in disabled]
        suggestions = []
        for file in files:
            if file.status == "removed" or not file.patch:
                continue
            # works from the patch alone, so files whose full content couldn't be fetched are
            # still checked
            suggestions.extend(self.scan_secrets(file, secrets))
            suggestions.extend(self.scan_dangerous(file, dangerous))
        suggestions.extend(self._run_tools(files, disabled))

        unique = deduplicate(suggestions)
        counts = Counter(s.severity for s in unique)
        log.info(
            "security scan complete",
            total=len(unique),
            critical=counts["CRITICAL"],
            high=counts["HIGH"],
            medium=counts["MEDIUM"],
            low=counts["LOW"],
        )
        return unique

    def _run_tools(self, files: list[FileChange], disabled: Collection[str]) -> list[Suggestion]:
        # the external tools run once per PR, not once per file
        suggestions: list[Suggestion] = []
        self.warnings = []
        if SEMGREP_RULE not in disabled:
            suggestions.extend(self.semgrep.scan(files))
            self.warnings += self.semgrep.warnings
        if DEPENDENCY_AUDIT_RULE not in disabled:
            suggestions.extend(self.dep_auditor.scan(files))
            self.warnings += self.dep_auditor.warnings
        return suggestions

    def scan_secrets(
        self, file: FileChange, patterns: list[dict[str, Any]] = SECRET_PATTERNS
    ) -> list[Suggestion]:
        if file.patch is None:
            return []

        suggestions = []
        for line_no, text in iter_added_lines(file.patch):
            for pattern in patterns:
                match = pattern["regex"].search(text)
                if match is None:
                    continue
                group = pattern.get("value_group")
                if group is not None and PLACEHOLDER.match(match.group(group)):
                    continue
                suggestions.append(self._suggestion(file, pattern, line_no))

        return suggestions

    def scan_dangerous(
        self, file: FileChange, patterns: list[dict[str, Any]] = DANGEROUS_PATTERNS
    ) -> list[Suggestion]:
        extension = file_extension(file.filename)
        if file.patch is None or extension not in CODE_EXTENSIONS:
            return []

        suggestions = []
        for line_no, text in iter_added_lines(file.patch):
            code = split_line(text, extension).code
            for pattern in patterns:
                if pattern["regex"].search(code):
                    suggestions.append(self._suggestion(file, pattern, line_no))

        return suggestions

    @staticmethod
    def _suggestion(file: FileChange, pattern: dict, line_no: int) -> Suggestion:
        return Suggestion(
            filename=file.filename,
            severity=pattern["severity"],
            category=pattern["category"],
            title=pattern["title"],
            description=pattern["description"],
            line=line_no,
            source="security",
            rule_id=pattern["id"],
        )
