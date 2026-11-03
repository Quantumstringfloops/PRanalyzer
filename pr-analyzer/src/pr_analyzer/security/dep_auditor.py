import json
import os
import re
import subprocess
import tempfile

import structlog

from pr_analyzer.config import settings
from pr_analyzer.diff import iter_added_lines
from pr_analyzer.models import FileChange, Suggestion
from pr_analyzer.security import tools

log = structlog.get_logger(__name__)

# the stage's id in .pr-analyzer.toml
DEPENDENCY_AUDIT_RULE = "dependency-audit"

# Only exact pins (`name==version`, optionally with extras or an environment marker) are audited.
# Anything else - ranges, URLs, VCS links, -r/-e/--index-url options - is skipped: handing it to
# pip would make the analyzer download and build packages chosen by the PR's author.
PINNED_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*(?:;.*)?$"
)


def is_requirements_file(filename: str) -> bool:
    # requirements.txt, requirements-dev.txt, dev-requirements.txt, requirements/base.txt, ...
    parts = filename.replace("\\", "/").lower().split("/")
    name = parts[-1]
    in_requirements_dir = len(parts) > 1 and parts[-2] == "requirements"
    return name.endswith(".txt") and ("requirements" in name or in_requirements_dir)


def canonical_name(name: str) -> str:
    # PEP 503 normalisation, so "Jinja2" in the file matches "jinja2" in pip-audit's output
    return re.sub(r"[-_.]+", "-", name).lower()


class DepAuditScanner:

    def __init__(self, timeout: int | None = None):
        self.timeout = timeout or settings.pip_audit_timeout_seconds
        self.warnings: list[str] = []

    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        self.warnings = []
        suggestions = []

        for file in files:
            if file.status == "removed" or not file.patch or not is_requirements_file(file.filename):
                continue

            pins = self._extract_added_pins(file)
            if not pins:
                continue

            executable = tools.find_tool("pip-audit")
            if executable is None:
                self._warn("Dependency audit skipped: pip-audit is not installed "
                           "(pip install 'pranalyzer[security]')")
                break

            log.info("running pip-audit", filename=file.filename, packages=len(pins))
            requirements = [requirement for requirement, _ in pins.values()]
            output = self._run_pip_audit(executable, requirements)
            suggestions.extend(self._parse_audit_output(output, file.filename, pins))

        log.info("dep audit complete", total_suggestions=len(suggestions))
        return suggestions

    def _extract_added_pins(self, file: FileChange) -> dict[str, tuple[str, int]]:
        # canonical package name -> ("name==version", line number in the new file)
        pins = {}
        skipped = 0
        for line_no, text in iter_added_lines(file.patch or ""):
            requirement = re.sub(r"\s+#.*$", "", text).strip()   # pip comments need a space before #
            if not requirement or requirement.startswith("#"):
                continue
            match = PINNED_REQUIREMENT.match(requirement)
            if match is None:
                skipped += 1
                continue
            name, version = match.group("name"), match.group("version")
            pins[canonical_name(name)] = (f"{name}=={version}", line_no)

        if skipped:
            log.info("unpinned_requirements_skipped", filename=file.filename, count=skipped)
        return pins

    def _run_pip_audit(self, executable: str, requirements: list[str]) -> dict:
        with tempfile.TemporaryDirectory(prefix="pr-analyzer-pip-audit-") as workdir:
            path = os.path.join(workdir, "requirements.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(requirements) + "\n")

            # --no-deps --disable-pip: look the pinned versions up in the advisory database
            # without installing, building or resolving anything
            command = [
                executable, "-r", path, "--no-deps", "--disable-pip",
                "--format", "json", "--progress-spinner", "off",
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=tools.scrubbed_env(),
                )
            except subprocess.TimeoutExpired:
                self._warn(f"Dependency audit skipped: pip-audit took longer than {self.timeout}s")
                return {}
            except OSError as e:
                self._warn(f"Dependency audit skipped: could not run pip-audit ({e})")
                return {}

        # pip-audit exits 1 both when it finds vulnerabilities and when it fails, so trust the
        # JSON report rather than the exit code
        try:
            output = json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            output = None
        if not isinstance(output, dict):
            self._warn(f"Dependency audit failed (exit {result.returncode}): "
                       f"{result.stderr.strip()[:200]}")
            return {}
        return output

    def _parse_audit_output(
        self, output: dict, filename: str, pins: dict[str, tuple[str, int]]
    ) -> list[Suggestion]:
        # pip-audit output structure:
        # output
        #   dependencies[]
        #     name     → package name e.g. "flask"
        #     version  → pinned version e.g. "0.12.2"
        #     vulns[]
        #       id          → PYSEC/GHSA id
        #       aliases[]   → other ids for the same advisory, e.g. a CVE
        #       description → what the vulnerability is
        #       fix_versions[] → versions that fix it
        suggestions = []
        seen = set()  # pip-audit can list the same advisory more than once
        for dep in output.get("dependencies") or []:
            name = dep.get("name", "unknown")
            version = dep.get("version", "")
            _, line = pins.get(canonical_name(name), ("", None))

            for vuln in dep.get("vulns") or []:
                vuln_id = vuln.get("id", "unknown")
                key = (canonical_name(name), version, vuln_id)
                if key in seen:
                    continue
                seen.add(key)
                aliases = [a for a in vuln.get("aliases") or [] if a != vuln_id]
                fix_versions = vuln.get("fix_versions") or []
                alias_text = f" (also {', '.join(aliases[:3])})" if aliases else ""
                fix_text = f" Fix: upgrade to {fix_versions[0]}." if fix_versions else ""
                description = (vuln.get("description") or "").strip()[:300]

                suggestions.append(Suggestion(
                    filename=filename,
                    severity="HIGH",
                    category="security",
                    # one suggestion per advisory: the id keeps several advisories for the same
                    # package from being collapsed into one
                    title=f"Vulnerable dependency: {name}=={version} ({vuln_id})",
                    description=f"{vuln_id}{alias_text}: {description}{fix_text}",
                    line=line,
                    source="security",
                    rule_id=DEPENDENCY_AUDIT_RULE,
                ))
        return suggestions

    def _warn(self, message: str) -> None:
        log.warning("pip_audit", problem=message)
        self.warnings.append(message)
