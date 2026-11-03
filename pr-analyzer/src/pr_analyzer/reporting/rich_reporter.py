import json
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from pr_analyzer.models import AnalysisResult, Suggestion

console = Console()

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

SEVERITY_COLOURS = {
    "CRITICAL": "white on red",
    "HIGH":     "bold red",
    "MEDIUM":   "yellow",
    "LOW":      "blue",
}

# PR titles, file names and LLM text are untrusted: they're escaped (or wrapped in Text) before
# Rich renders them, otherwise "[docs] Fix" loses its "[docs]" and "[/x]" crashes the report.


class RichReporter:

    def print_rich_report(self, result: AnalysisResult) -> None:
        self._print_pr_header(result)
        self._print_score_panel(result)
        self._print_warnings(result)
        self._print_dimension_table(result)
        sorted_suggestions = self._sort_suggestions(result.suggestions)
        self._print_suggestion_table(sorted_suggestions)
        self._print_detail_panels(sorted_suggestions)
        self._print_final_line(result)

    def print_json_report(self, result: AnalysisResult) -> None:
        output = {
            "pr": {
                "number":     result.pr.number,
                "title":      result.pr.title,
                "author":     result.pr.author,
                "repo":       result.pr.repo,
                "base":       result.pr.base_branch,
                "head":       result.pr.head_branch,
                "head_sha":   result.pr.head_sha,
            },
            "score": {
                "overall":    result.score.overall_score,
                "grade":      result.score.grade,
                "passed":     result.score.passed,
                "summary":    result.score.summary,
                "delta":      result.score.delta,
                "dimensions": result.score.dimension_scores,
                "critical":   result.score.critical_count,
                "high":       result.score.high_count,
            },
            "suggestions": [
                {
                    "filename":    s.filename,
                    "line":        s.line,
                    "severity":    s.severity,
                    "category":    s.category,
                    "title":       s.title,
                    "description": s.description,
                    "source":      s.source,
                    "confidence":  s.confidence,
                    "rule":        s.rule_id,   # the id .pr-analyzer.toml can disable
                }
                for s in result.suggestions
            ],
            "warnings": result.warnings,
            "llm_provider": result.llm_provider_used,
            "duration": result.duration,
        }
        print(json.dumps(output, indent=2))

    def print_markdown_report(self, result: AnalysisResult) -> None:
        lines = []

        lines.append(f"# PR Analysis Report — #{result.pr.number} {_md(result.pr.title)}\n")
        lines.append(f"**Author:** {_md(result.pr.author)}  ")
        lines.append(f"**Repo:** {_md(result.pr.repo)}  ")
        lines.append(f"**Branch:** `{result.pr.head_branch}` → `{result.pr.base_branch}`\n")

        lines.append("## Score\n")
        lines.append("| Overall | Grade | Status | Critical | High |")
        lines.append("|---------|-------|--------|----------|------|")
        status = "✅ PASSED" if result.score.passed else "❌ BLOCKED"
        lines.append(
            f"| {result.score.overall_score:.1f} "
            f"| {result.score.grade} "
            f"| {status} "
            f"| {result.score.critical_count} "
            f"| {result.score.high_count} |"
        )

        if result.warnings:
            lines.append("\n## Warnings\n")
            lines.extend(f"- {_md(w)}" for w in result.warnings)

        lines.append("\n## Dimension Scores\n")
        lines.append("| Dimension | Score |")
        lines.append("|-----------|-------|")
        for dim, score in result.score.dimension_scores.items():
            lines.append(f"| {dim} | {score:.1f} |")

        if result.suggestions:
            lines.append("\n## Suggestions\n")
            lines.append("| Severity | File | Line | Category | Title |")
            lines.append("|----------|------|------|----------|-------|")
            for s in self._sort_suggestions(result.suggestions):
                line = str(s.line) if s.line else "—"
                lines.append(f"| {s.severity} | {_md(s.filename)} | {line} | {_md(s.category)} | {_md(s.title)} |")

            lines.append("\n## Details\n")
            for s in self._sort_suggestions(result.suggestions):
                if s.severity in ("HIGH", "CRITICAL"):
                    where = f" line {s.line}" if s.line else ""
                    lines.append(f"### {s.severity} — {_md(s.title)}")
                    lines.append(f"**File:** `{s.filename}`{where}  ")
                    lines.append(f"**Source:** {s.source}  ")
                    lines.append(f"> {_md(s.description)}\n")

        lines.append(f"\n---\n*Generated by pr-analyzer — duration {result.duration:.2f}s*")
        print("\n".join(lines))

    def _sort_suggestions(self, suggestions: list[Suggestion]) -> list[Suggestion]:
        return sorted(suggestions, key=lambda s: SEVERITY_ORDER.get(s.severity, 99))

    def _severity_colour(self, severity: str) -> str:
        return SEVERITY_COLOURS.get(severity, "white")

    def _print_pr_header(self, result: AnalysisResult) -> None:
        pr = result.pr
        content = (
            f"[bold]Author:[/bold] {escape(pr.author)}\n"
            f"[bold]Repo:[/bold] {escape(pr.repo)}\n"
            f"[bold]Branch:[/bold] {escape(pr.head_branch)} → {escape(pr.base_branch)}\n"
            f"[bold]Draft:[/bold] {'Yes' if pr.is_draft else 'No'}\n"
            f"[bold]Files:[/bold] {len(pr.files)}  "
            f"[green]+{pr.total_additions}[/green]  "
            f"[red]-{pr.total_deletions}[/red]"
        )
        title = f"PR #{pr.number} — {escape(pr.title)}"
        console.print(Panel(content, title=title, border_style="blue"))

    def _print_score_panel(self, result: AnalysisResult) -> None:
        score = result.score
        status_colour = "green" if score.passed else "red"
        status_text = "✓ PASSED" if score.passed else "✗ BLOCKED"

        delta_text = ""
        if score.delta is not None:
            delta_colour = "green" if score.delta >= 0 else "red"
            delta_text = f"\n[bold]Delta:[/bold] [{delta_colour}]{score.delta:+.1f}[/{delta_colour}]"

        content = (
            f"[bold]Score:[/bold] {score.overall_score:.1f}   "
            f"[bold]Grade:[/bold] {score.grade}   "
            f"[{status_colour}][bold]{status_text}[/bold][/{status_colour}]"
            f"{delta_text}\n"
            f"[italic]{escape(score.summary)}[/italic]"
        )
        console.print(Panel(content, title="Quality Score", border_style=status_colour))

    def _print_warnings(self, result: AnalysisResult) -> None:
        if not result.warnings:
            return
        content = "\n".join(f"• {escape(w)}" for w in result.warnings)
        console.print(Panel(content, title="Warnings", border_style="yellow"))

    def _print_dimension_table(self, result: AnalysisResult) -> None:
        table = Table(title="Dimension Scores", show_lines=True)
        table.add_column("Dimension", style="bold")
        table.add_column("Score", justify="right")
        table.add_column("Bar", width=20)

        for dim, score in result.score.dimension_scores.items():
            if score >= 80:
                colour = "green"
            elif score >= 60:
                colour = "yellow"
            else:
                colour = "red"

            filled = int(score / 5)
            bar = "█" * filled + "░" * (20 - filled)

            table.add_row(dim, f"[{colour}]{score:.1f}[/{colour}]", f"[{colour}]{bar}[/{colour}]")

        console.print(table)

    def _print_suggestion_table(self, suggestions: list[Suggestion]) -> None:
        if not suggestions:
            console.print("[green]No suggestions — clean code![/green]")
            return

        # no fixed widths: they cut "CRITICAL" down to "CRIT…" on 80-column CI logs
        table = Table(title="Suggestions", show_lines=True)
        table.add_column("Severity", no_wrap=True)
        table.add_column("File", overflow="fold")
        table.add_column("Category", no_wrap=True)
        table.add_column("Title", overflow="fold")

        for s in suggestions:
            colour = self._severity_colour(s.severity)
            file_line = f"{s.filename}:{s.line}" if s.line else s.filename
            table.add_row(
                Text(s.severity, style=colour),
                Text(file_line),
                Text(s.category),
                Text(s.title),
            )

        console.print(table)

    def _print_detail_panels(self, suggestions: list[Suggestion]) -> None:
        for s in suggestions:
            if s.severity not in ("HIGH", "CRITICAL"):
                continue
            content = (
                f"[bold]Description:[/bold] {escape(s.description)}\n"
                f"[bold]Source:[/bold] {escape(s.source)}\n"
                f"[bold]Category:[/bold] {escape(s.category)}"
            )
            title = f"{s.severity} — {escape(s.filename)}" + (f" line {s.line}" if s.line else "")
            console.print(Panel(content, title=title, border_style="red"))

    def _print_final_line(self, result: AnalysisResult) -> None:
        if result.score.passed:
            status = "[bold green]✓ PASSED[/bold green]"
        else:
            status = "[bold red]✗ BLOCKED[/bold red]"

        console.print(
            f"\n{status}  "
            f"[dim]{len(result.suggestions)} suggestion(s)  "
            f"duration {result.duration:.2f}s[/dim]"
        )


def _md(text: str) -> str:
    # keep untrusted text from breaking Markdown tables or blockquotes
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
