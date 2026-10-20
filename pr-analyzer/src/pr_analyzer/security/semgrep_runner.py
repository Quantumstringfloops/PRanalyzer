import json
import subprocess
import tempfile
import os
from pr_analyzer.models import FileChange, Suggestion

class SemgrepScanner:

    def scan(self, files: list[FileChange]) -> list[Suggestion]:
        # loops files
        # skips removed files and files with no raw_content
        # writes raw_content to a temp file
        # calls _run_semgrep() on that temp file
        # calls _parse_sarif() on the output
        # collects all suggestions
        # returns combined list
        pass

    def _run_semgrep(self, filepath: str) -> dict:
        # builds the subprocess command:
        #   semgrep --config p/owasp-top-ten --sarif <filepath>
        # runs it with subprocess.run()
        # captures stdout
        # parses stdout as JSON
        # returns the parsed dict
        # returns {} on failure
        pass

    def _parse_sarif(self, sarif: dict, filename: str) -> list[Suggestion]:
        # navigates sarif["runs"][0]["results"]
        # for each result extracts:
        #   rule_id → title
        #   message.text → description
        #   locations[0].physicalLocation.region.startLine → line
        # maps semgrep severity to your severity levels
        # builds and returns list of Suggestion objects
        pass

    def _map_severity(self, semgrep_level: str) -> str:
        # semgrep uses: ERROR, WARNING, NOTE
        # maps to your: CRITICAL/HIGH, MEDIUM, LOW
        pass