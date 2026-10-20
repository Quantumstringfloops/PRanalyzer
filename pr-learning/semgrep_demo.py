# learning_semgrep_depaudit.py
# Run this file directly: python learning_semgrep_depaudit.py
# It does NOT connect to GitHub — uses hardcoded fake data

import json
import subprocess
import tempfile
import os

# ── fake file content to scan ─────────────────────────────────────────────────
# this simulates what raw_content looks like from a real PR file
FAKE_FILE_CONTENT = """
import yaml
import pickle

def load_config(data):
    return yaml.load(data)       # unsafe — no SafeLoader

def load_model(f):
    return pickle.load(f)        # dangerous deserialization

password = "supersecret123"      # hardcoded secret
"""

# ── fake requirements diff ────────────────────────────────────────────────────
# this simulates what file.patch looks like for a requirements.txt change
FAKE_REQUIREMENTS_PATCH = """
@@ -1,2 +1,4 @@
 requests==2.28.0
+flask==0.12.2
+django==2.0.0
-old-package==1.0.0
"""

# ─────────────────────────────────────────────────────────────────────────────
# PART 1 — SEMGREP
# ─────────────────────────────────────────────────────────────────────────────

def run_semgrep(filepath: str) -> dict:
    # builds the command and runs it
    # --config p/owasp-top-ten → use OWASP ruleset
    # --sarif → output in SARIF JSON format
    # --quiet → no progress output
    result = subprocess.run(
        ["semgrep", "--config", "p/owasp-top-ten", "--sarif", "--quiet", filepath],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print("semgrep failed:", result.stderr)
        return {}
    return json.loads(result.stdout)


def parse_sarif(sarif: dict, filename: str) -> list[dict]:
    # navigates the SARIF structure and pulls out findings
    findings = []
    runs = sarif.get("runs", [])
    if not runs:
        return []

    for result in runs[0].get("results", []):
        rule_id = result.get("ruleId", "unknown")
        message = result.get("message", {}).get("text", "")
        locations = result.get("locations", [])
        line = None
        if locations:
            line = locations[0]["physicalLocation"]["region"]["startLine"]

        findings.append({
            "filename": filename,
            "rule": rule_id,
            "message": message,
            "line": line,
        })
    return findings


def semgrep_demo():
    print("\n── SEMGREP DEMO ──────────────────────────────────────────")

    # write fake content to a temp file
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(FAKE_FILE_CONTENT)
        temp_path = f.name

    print(f"wrote temp file: {temp_path}")

    sarif = run_semgrep(temp_path)
    findings = parse_sarif(sarif, "fake_file.py")

    print(f"found {len(findings)} issues:")
    for f in findings:
        print(f"  line {f['line']} — {f['rule']}: {f['message'][:60]}")

    os.unlink(temp_path)  # clean up temp file


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — PIP AUDIT
# ─────────────────────────────────────────────────────────────────────────────

def extract_added_packages(patch: str) -> list[str]:
    # reads only "+" lines from the patch
    # skips "+++ filename" header lines
    packages = []
    for line in patch.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            packages.append(line[1:].strip())
    return packages


def run_pip_audit(packages: list[str]) -> dict:
    # writes packages to a temp requirements file
    # runs pip-audit against it
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("\n".join(packages))
        temp_path = f.name

    result = subprocess.run(
        ["pip-audit", "-r", temp_path, "--format", "json"],
        capture_output=True,
        text=True
    )
    os.unlink(temp_path)

    if result.returncode not in (0, 1):  # 1 means vulns found — still valid
        print("pip-audit failed:", result.stderr)
        return {}
    return json.loads(result.stdout)


def parse_audit_output(output: dict, filename: str) -> list[dict]:
    findings = []
    for dep in output.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            findings.append({
                "filename": filename,
                "package": dep["name"],
                "version": dep["version"],
                "cve": vuln["id"],
                "description": vuln["description"][:80],
                "fix": vuln.get("fix_versions", []),
            })
    return findings


def dep_audit_demo():
    print("\n── DEP AUDIT DEMO ────────────────────────────────────────")

    packages = extract_added_packages(FAKE_REQUIREMENTS_PATCH)
    print(f"added packages found: {packages}")

    output = run_pip_audit(packages)
    findings = parse_audit_output(output, "requirements.txt")

    print(f"found {len(findings)} vulnerabilities:")
    for f in findings:
        print(f"  {f['package']}=={f['version']} — {f['cve']}: {f['description']}")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    semgrep_demo()
    dep_audit_demo()