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
    lines = diff.splitlines()
    if len(lines) > max_lines:
          lines = lines[:max_lines]

    return "\n".join(lines)


def build_prompt(pr: PRSummary,diff: str,existing_findings: list[Suggestion],max_diff_lines: int):
    truncated_diff = truncate_diff(diff)
    pr_info = f"""
        PR #{pr.number}: {pr.title}
        Author: {pr.author}
        Repo: {pr.repo}
        Branch: {pr.head_branch} → {pr.base_branch}
        Files changed: {len(pr.files)}
        Total additions: {pr.total_additions}  deletions: {pr.total_deletions}
        """

    file_lines = []
    for f in pr.files:
        file_lines.append(f"  {f.filename} ({f.status}) +{f.additions} -{f.deletions}")
    file_summary = "Changed files:\n" + "\n".join(file_lines)

    raw_diff = ""
    for f in pr.files:
         if f.patch:
              raw_diff = f"\n--- {f.filename} ---\n{f.patch}\n"
    diff_section = "Diff:\n" + truncate_diff(raw_diff, max_diff_lines)


    if existing_findings:
        finding_lines = []
        for s in existing_findings:
            finding_lines.append(
                f"  [{s.severity}] {s.filename} line {s.line}: {s.title}"
            )
        findings_section = "Already found by static/security scanners (do not repeat these):\n" + "\n".join(finding_lines)
    else:
        findings_section = "No existing findings."

    # ── assemble ──────────────────────────────────────────────────────────────
    return "\n\n".join([pr_info, file_summary, diff_section, findings_section])
