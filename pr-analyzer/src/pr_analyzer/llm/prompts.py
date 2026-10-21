from pr_analyzer.models import PRSummary,Suggestion
from config import settings
import structlog

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """
You are a pull-request code reviewer.

Return only a valid JSON array. Do not use Markdown code fences,
headings, explanations, or text outside the array.

Each array element must contain exactly these fields:
- category: string
- severity: "low", "medium", or "high"
- title: string
- description: string
- file: string
- line_start: integer or null
- confidence: number between 0 and 1

If there are no useful suggestions, return [].
Do not report formatting preferences unless they affect correctness,
security, maintainability, or reliability.
""".strip()


def truncate_diff(diff:str, max_lines: str) -> str:
    raw =""
    lines = diff.splitlines()
    if len(lines) > max_lines:
          lines = lines[:max_lines]

    raw = "\n".join(lines)


    return raw


def build_prompt(pr: PRSummary,diff: str,existing_findings: list[Suggestion],max_diff_lines: int):
    truncated_diff = truncate_diff(diff)
    user_prompt = ""

    values = []
    
    string = f"{pr.title}, {pr.author}, {pr.repo}, {pr.head_branch}, {pr.base_branch}, {pr.total_additions}"
