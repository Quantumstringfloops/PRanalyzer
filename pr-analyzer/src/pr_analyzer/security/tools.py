"""Finding and preparing the external scanners (semgrep, pip-audit)."""
import os
import shutil
import sys

# credentials that must never reach tools which process untrusted PR content
SENSITIVE_ENV_VARS = {"GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}


def find_tool(name: str) -> str | None:
    """Look next to the running interpreter first, so tools installed in the same virtualenv
    are found even when that virtualenv isn't activated, then on PATH."""
    search_path = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
    return shutil.which(name, path=search_path)


def scrubbed_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SENSITIVE_ENV_VARS and not key.endswith("_API_KEY")
    }
