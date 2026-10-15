from dataclasses import dataclass, field


@dataclass
class FileChange:
    filename: str
    status: str           # "added" | "modified" | "removed" | "renamed"
    additions: int
    deletions: int
    patch: str | None     # raw diff text — the +/- lines
    raw_content: str | None = None  # full file, used later by tree-sitter


@dataclass
class PRSummary:
    number: int
    title: str
    author: str
    repo: str             # "microsoft/vscode"
    base_branch: str      # branch being merged into
    head_branch: str      # branch with the changes
    total_additions: int
    total_deletions: int
    is_draft: bool
    files: list[FileChange] = field(default_factory=list)


@dataclass
class Suggestion:
    filename: str
    severity: str         # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    category: str         # "style" | "security" | "complexity" | "performance" | "documentation" | "testing"
    title: str            # short label shown in the table
    description: str      # longer explanation shown in detail panel
    line: int | None = None       # line number in the file if known
    source: str = "static"        # "static" | "security" | "llm"