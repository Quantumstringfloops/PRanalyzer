import json
import subprocess
import tempfile
import os
import structlog
from pr_analyzer.models import FileChange, Suggestion

log = structlog.get_logger(__name__)

# filenames that count as dependency files
REQUIREMENTS_FILES = {"requirements.txt", "requirements-dev.txt", "requirements_dev.txt"}

class DepAuditScanner:

    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        suggestions = []

        for file in files:
            # only care about requirements files
            basename = os.path.basename(file.filename)
            if basename not in REQUIREMENTS_FILES:
                continue
            if file.patch is None:
                continue

            packages = self._extract_added_packages(file)
            if not packages:
                continue

            log.info("running pip-audit", filename=file.filename, packages=packages)
            output = self._run_pip_audit(packages)
            suggestions.extend(self._parse_audit_output(output, file.filename))

        log.info("dep audit complete", total_suggestions=len(suggestions))
        return suggestions

    def _extract_added_packages(self, file: FileChange) -> list[str]:
        # reads only "+" lines from the patch
        # these are packages being added in this PR
        # skips "+++ filename" header and blank lines
        packages = []
        for line in file.patch.splitlines():
            if line.startswith("+++"):
                continue
            if line.startswith("+"):
                package = line[1:].strip()
                if package and not package.startswith("#"):
                    packages.append(package)
        return packages

    def _run_pip_audit(self, packages: list[str]) -> dict:
        # write packages to a temp requirements file
        # pip-audit needs a file — it can't accept a list directly
        with tempfile.NamedTemporaryFile(
            suffix=".txt",
            mode="w",
            delete=False
        ) as f:
            f.write("\n".join(packages))
            temp_path = f.name

        result = subprocess.run(
            ["pip-audit", "-r", temp_path, "--format", "json", "--progress-spinner", "off"],
            capture_output=True,
            text=True
        )
        os.unlink(temp_path)

        if result.returncode not in (0, 1):
            log.warning("pip-audit failed", error=result.stderr[:200])
            return {}
        if not result.stdout:
            return {}
        return json.loads(result.stdout)

    def _parse_audit_output(self, output: dict, filename: str) -> list[Suggestion]:
        # pip-audit output structure:
        # output
        #   dependencies[]
        #     name     → package name e.g. "flask"
        #     version  → installed version e.g. "0.12.2"
        #     vulns[]
        #       id          → CVE or PYSEC id
        #       description → what the vulnerability is
        #       fix_versions[] → versions that fix it
        suggestions = []
        seen = set()  # track CVE ids to avoid duplicates

        for dep in output.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                cve_id = vuln.get("id", "")

                # skip duplicate CVEs from multiple advisory sources
                key = f"{dep['name']}-{cve_id}"
                if key in seen:
                    continue
                seen.add(key)

                fix_versions = vuln.get("fix_versions", [])
                fix_text = f" Fix: upgrade to {fix_versions[0]}" if fix_versions else ""

                suggestions.append(Suggestion(
                    filename=filename,
                    severity="HIGH",
                    category="security",
                    title=f"Vulnerable dependency: {dep['name']}=={dep['version']}",
                    description=f"{cve_id}: {vuln.get('description', '')[:200]}{fix_text}",
                    line=None,
                    source="security"
                ))

        return suggestions