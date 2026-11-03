import json
import os
import subprocess
import tempfile
from urllib.parse import unquote

import structlog

from pr_analyzer.config import settings
from pr_analyzer.diff import iter_added_lines
from pr_analyzer.models import FileChange, Suggestion
from pr_analyzer.security import tools

log = structlog.get_logger(__name__)

# the stage's id in .pr-analyzer.toml; Semgrep's own rule id is in each finding's title
SEMGREP_RULE = "semgrep"


class SemgrepRunner:

    def __init__(self, config: str | None = None, timeout: int | None = None):
        self.config = config or settings.semgrep_config
        self.timeout = timeout or settings.semgrep_timeout_seconds
        self.warnings: list[str] = []

    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        self.warnings = []
        targets = [
            f for f in files
            if f.status != "removed" and f.raw_content is not None and f.patch
        ]
        if not targets:
            return []

        executable = tools.find_tool("semgrep")
        if executable is None:
            self._warn("Semgrep scan skipped: semgrep is not installed "
                       "(pip install 'pranalyzer[security]')")
            return []

        # one run over a copy of every changed file, laid out with its repo path so findings map
        # straight back to the right file
        with tempfile.TemporaryDirectory(prefix="pr-analyzer-semgrep-") as workdir:
            files_by_path = {}
            for file in targets:
                relative = safe_relative_path(file.filename)
                if relative is None:
                    log.warning("semgrep_skipped_path", filename=file.filename)
                    continue
                destination = os.path.join(workdir, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "w", encoding="utf-8") as handle:
                    handle.write(file.raw_content or "")
                files_by_path[relative] = file

            if not files_by_path:
                return []
            sarif = self.run_semgrep(executable, workdir)
            suggestions = self.parse_sarif(sarif, files_by_path, root=workdir)

        log.info("semgrep scan complete", total_suggestions=len(suggestions))
        return suggestions

    def run_semgrep(self, executable: str, target_dir: str) -> dict:
        command = [
            executable, "scan",
            "--config", self.config,
            "--sarif", "--quiet",
            "--metrics", "off",
            "--disable-version-check",
            ".",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=tools.scrubbed_env(),
            )
        except subprocess.TimeoutExpired:
            self._warn(f"Semgrep scan skipped: it took longer than {self.timeout}s")
            return {}
        except OSError as e:
            self._warn(f"Semgrep scan skipped: could not run semgrep ({e})")
            return {}

        if result.returncode not in (0, 1):
            self._warn(f"Semgrep scan failed (exit {result.returncode}): "
                       f"{result.stderr.strip()[:200]}")
            return {}
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self._warn("Semgrep scan failed: its output was not valid SARIF JSON")
            return {}

    def parse_sarif(
        self, sarif: dict, files_by_path: dict[str, FileChange], root: str | None = None
    ) -> list[Suggestion]:
        runs = sarif.get("runs") or []
        if not runs:
            return []
        run = runs[0]
        levels = rule_levels(run)

        added_lines: dict[str, set[int]] = {}
        suggestions = []
        for result in run.get("results") or []:
            location = result_location(result)
            if location is None:
                continue
            uri, start, end = location
            file = files_by_path.get(sarif_path(uri, root))
            if file is None:
                continue

            # report only what the PR added; issues already in the file aren't this PR's doing
            if file.filename not in added_lines:
                added_lines[file.filename] = {n for n, _ in iter_added_lines(file.patch or "")}
            if not any(n in added_lines[file.filename] for n in range(start, end + 1)):
                continue

            rule_id = result.get("ruleId", "unknown")
            level = result.get("level") or levels.get(rule_id, "warning")
            message = (result.get("message") or {}).get("text", "")
            suggestions.append(Suggestion(
                filename=file.filename,
                severity=self.map_severity(level),
                category="security",
                title=f"Semgrep: {rule_id.rsplit('.', 1)[-1]}",
                description=f"{message} (rule: {rule_id})",
                line=start,
                source="security",
                rule_id=SEMGREP_RULE,
            ))
        return suggestions

    def map_severity(self, level: str) -> str:
        mapping = {
            "error": "HIGH",
            "warning": "MEDIUM",
            "note": "LOW",
            "none": "LOW",
        }
        return mapping.get(level.lower(), "MEDIUM")

    def _warn(self, message: str) -> None:
        log.warning("semgrep", problem=message)
        self.warnings.append(message)


def rule_levels(run: dict) -> dict[str, str]:
    # semgrep puts the severity on the rule (defaultConfiguration.level), not on each result
    rules = (run.get("tool") or {}).get("driver", {}).get("rules") or []
    return {
        rule.get("id"): (rule.get("defaultConfiguration") or {}).get("level", "warning")
        for rule in rules
    }


def result_location(result: dict) -> tuple[str, int, int] | None:
    """(uri, first line, last line) of a SARIF result, or None if it names no line."""
    location = ((result.get("locations") or [{}])[0]).get("physicalLocation") or {}
    uri = (location.get("artifactLocation") or {}).get("uri", "")
    region = location.get("region") or {}
    start = region.get("startLine")
    if not isinstance(start, int):
        return None
    end = region.get("endLine")
    return uri, start, end if isinstance(end, int) else start


def safe_relative_path(filename: str) -> str | None:
    """The file's repo path, or None if it could escape the scan directory."""
    normalized = os.path.normpath(filename.replace("\\", "/"))
    if os.path.isabs(normalized) or normalized in (".", "..") or normalized.startswith(".." + os.sep):
        return None
    return normalized


def sarif_path(uri: str, root: str | None) -> str:
    path = unquote(uri[len("file://"):] if uri.startswith("file://") else uri)
    if os.path.isabs(path) and root:
        path = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    return os.path.normpath(path)
