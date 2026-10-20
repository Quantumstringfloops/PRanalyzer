import json
import subprocess
import tempfile
import os
from pr_analyzer.models import FileChange, Suggestion
import structlog
log = structlog.get_logger(__name__)



class SemgrepRunner:

    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        # loops files
        suggestions = []
        for file in files:
            if file.status == "removed":
                continue
            if file.raw_content is None:
                continue
        
        with tempfile.NamedTemporaryFile(suffix=self.get_suffix(file.filename),mode="w",delete=False) as tmp_file:
            tmp_file.write(file.raw_content)
            temp_path = tmp_file.name
        try:
            sarif= self.run_semgrep(temp_path)
            suggestions.extend(self.parse_sarif(sarif, "fake_file.py"))
        finally:
            os.unlink(temp_path)
        log.info("semgrep scan complete", total_suggestions=len(suggestions))
        return suggestions

    def get_suffix(self, filename : str):
        ext = os.path.splitext(filename)[1]
        return ext if ext else ".py"
    
    def run_semgrep(self, filepath: str) -> dict:
        result = subprocess.run(
        ["semgrep", "--config", "p/owasp-top-ten", "--sarif", "--quiet", filepath],
        capture_output=True,
        text=True
        )
        if result.returncode not in (0,1):
            print("semgrep failed:", result.stderr[:200])
            return {}
        if not result.stdout:
            return {}
        
        return json.loads(result.stdout)
           

    def parse_sarif(self, sarif: dict, filename: str) -> list[Suggestion]:
        suggestions = []
        runs = sarif.get("runs", [])
        if not runs:
            return []

        for result in runs[0].get("results", []):
            rule_id = result.get("ruleId", "unknown")
            message = result.get("message", {}).get("text", "")
            level = result.get("level", "warning")
            locations = result.get("locations", [])
            line = None
            if locations:
                line = locations[0]["physicalLocation"]["region"]["startLine"]

            suggestions.append(Suggestion(
                filename= filename,
                severity= self.map_severity(level),
                category="security",
                title =f"Semgrep: {rule_id}",
                description=message,
                line=line,
                source="security"
            ))
        return suggestions

    def map_severity(self, level: str) -> str:
        mapping = {
            "error": "HIGH",
            "warning": "MEDIUM",
            "note": "LOW",
        }
        return mapping.get(level.lower(), "MEDIUM")