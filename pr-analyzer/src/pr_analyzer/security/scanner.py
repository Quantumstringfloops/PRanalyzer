import re 
from pr_analyzer.models import Suggestion, FileChange
import structlog
from pr_analyzer.security.semgrep_runner import SemgrepRunner
from pr_analyzer.security.dep_auditor import DepAuditScanner

log = structlog.get_logger(__name__)


SECRET_PATTERNS = [
    {
        "id": "aws_access_key",
        "regex": re.compile(r'AKIA[0-9A-Z]{16}', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "AWS access key exposed",
        "description": "An AWS access key was found hardcoded. Revoke it immediately and use environment variables.",
    },
    {
        "id": "generic_api_key",
        "regex": re.compile(r'(?i)(api_key|apikey|api_secret)\s*=\s*["\'][a-zA-Z0-9]{16,}["\']', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Hardcoded API key",
        "description": "An API key was found hardcoded. Move it to environment variables.",
    },
    {
        "id": "private_pem",
        "regex": re.compile(r'-----BEGIN (RSA |EC )?PRIVATE KEY-----', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Private key exposed",
        "description": "A private PEM key block was found. Remove it immediately and rotate the key.",
    },
    {
        "id": "github_pat",
        "regex": re.compile(r'gh[ps]_[a-zA-Z0-9]{36}', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "GitHub personal access token exposed",
        "description": "A GitHub PAT was found hardcoded. Revoke it immediately at github.com/settings/tokens.",
    },
    {
        "id": "jwt_token",
        "regex": re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "JWT token hardcoded",
        "description": "A JWT token was found hardcoded. Tokens should never be committed to source control.",
    },
    {
        "id": "hardcoded_password",
        "regex": re.compile(r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{4,}["\']', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Hardcoded password",
        "description": "A password was found hardcoded. Move it to environment variables.",
    },
    {
        "id": "db_connection_string",
        "regex": re.compile(r'(?i)(postgres|mysql|mongodb|redis):\/\/[^:]+:[^@]+@', re.MULTILINE),
        "severity": "CRITICAL",
        "category": "security",
        "title": "Database connection string with credentials",
        "description": "A database URL with embedded credentials was found. Use environment variables instead.",
    },
]

DANGEROUS_PATTERNS = [
    {
        "id": "sql_injection",
        "regex": re.compile(r'execute\s*\(\s*["\'].*%s.*["\']|execute\s*\(\s*f["\']', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "Possible SQL injection",
        "description": "SQL query built with string formatting. Use parameterised queries instead.",
    },
    {
        "id": "eval_usage",
        "regex": re.compile(r'\beval\s*\(', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "eval() usage detected",
        "description": "eval() executes arbitrary code. Never use it on user input.",
    },
    {
        "id": "pickle_load",
        "regex": re.compile(r'\bpickle\.load\s*\(', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "pickle.load() usage detected",
        "description": "pickle.load() can execute arbitrary code when loading untrusted data. Use JSON instead.",
    },
    {
        "id": "yaml_unsafe_load",
        "regex": re.compile(r'\byaml\.load\s*\([^)]*\)(?!\s*#.*safe)', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "yaml.load() without SafeLoader",
        "description": "yaml.load() is unsafe. Use yaml.safe_load() or pass Loader=yaml.SafeLoader.",
    },
    {
        "id": "subprocess_shell",
        "regex": re.compile(r'subprocess\.(run|call|Popen)\s*\(.*shell\s*=\s*True', re.MULTILINE),
        "severity": "HIGH",
        "category": "security",
        "title": "subprocess with shell=True",
        "description": "shell=True with user input allows command injection. Pass a list of arguments instead.",
    },
    {
        "id": "random_for_secrets",
        "regex": re.compile(r'\brandom\.(random|randint|choice|randrange)\s*\(', re.MULTILINE),
        "severity": "MEDIUM",
        "category": "security",
        "title": "Non-cryptographic random used",
        "description": "random module is not cryptographically secure. Use secrets module for tokens and passwords.",
    },
    {
        "id": "debug_mode",
        "regex": re.compile(r'\bdebug\s*=\s*True', re.MULTILINE | re.IGNORECASE),
        "severity": "MEDIUM",
        "category": "security",
        "title": "Debug mode enabled",
        "description": "debug=True should never be committed. It exposes stack traces and sensitive data in production.",
    },
]

def get_added_lines(patch:str) -> str:
    added_lines = ""
    if not patch : 
        return ""
    for line in patch.splitlines():
        if line.startswith('+++'):
            continue
        if line.startswith("+"):
            added_lines += line[1:] + "\n"

    return added_lines


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

class SecurityScanner:
    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        suggestions = []
        for file in files:
            if file.status == "removed":
                continue
            if file.raw_content is None:
                continue
            suggestions.extend(self.scan_secrets(file))
            suggestions.extend(self.scan_dangerous(file))
            suggestions.extend(SemgrepRunner().scan(files))
            suggestions.extend(DepAuditScanner().scan(files))

        seen = set()
        unique = []
        for s in suggestions:
            key = (s.filename, s.title, s.line)
            if key not in seen:
                seen.add(key)
                unique.append(s)

        # summary log
        critical= sum(1 for s in unique if s.severity == "CRITICAL")
        high= sum(1 for s in unique if s.severity == "HIGH")
        medium = sum(1 for s in unique if s.severity == "MEDIUM")
        low = sum(1 for s in unique if s.severity == "LOW")

        log.info(
            "security scan complete",
            total=len(unique),
            critical=critical,
            high=high,
            medium=medium,
            low=low,
        )

        return unique

            
    def scan_secrets(self, file: FileChange) -> list[Suggestion]:
        suggestions = []
        if file.patch is None:
            return []
        added_lines = get_added_lines(file.patch)

        for pattern in SECRET_PATTERNS:
            for match in pattern["regex"].finditer(added_lines):
                line = extract_line_numbers(file.patch, match.start())
                suggestions.append(Suggestion(
                    filename= file.filename,
                    severity=pattern["severity"],
                    category=pattern["category"],
                    title=pattern["title"],
                    description=pattern["description"],
                    line=line,
                    source="security"
                )) 

        return suggestions

    
    def scan_dangerous(self, file: FileChange) -> list[Suggestion]:
        suggestions = []
        if file.patch is None:
            return []
        added_lines = get_added_lines(file.patch)

        for pattern in DANGEROUS_PATTERNS:
            for match in pattern["regex"].finditer(added_lines):
                line = extract_line_numbers(file.patch, match.start())
                suggestions.append(Suggestion(
                    filename= file.filename,
                    severity=pattern["severity"],
                    category=pattern["category"],
                    title=pattern["title"],
                    description=pattern["description"],
                    line=line,
                    source="security"
                ))

        return suggestions
        
