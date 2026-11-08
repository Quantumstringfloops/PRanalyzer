"""The action's LLM inputs, and this repository's own PR Analysis workflow, which runs it."""
import re
from pathlib import Path

from pr_analyzer.llm.engine import DEFAULT_MODELS

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / "action.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-analysis.yml"
READMES = (ROOT / "README.md", ROOT / "pr-analyzer" / "README.md")
# every provider takes a key but Ollama, which runs where the action does
KEYED_PROVIDERS = sorted(provider for provider in DEFAULT_MODELS if provider != "ollama")


def action_inputs() -> set[str]:
    text = ACTION.read_text(encoding="utf-8")
    section = text.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
    return set(re.findall(r"^  ([a-z][a-z-]*):$", section, flags=re.MULTILINE))


def step_env(step_id: str) -> dict[str, str]:
    """The env: block of a step, as names and the expressions they're set to."""
    step = ACTION.read_text(encoding="utf-8").split(f"      id: {step_id}\n", 1)[1]
    env = step.split("      run: |", 1)[0]
    return dict(re.findall(r"^        ([A-Z_]+): (.+)$", env, flags=re.MULTILINE))


def test_every_provider_with_a_key_has_an_input_that_reaches_pr_analyzer():
    # a provider pr-analyzer knows but the action can't give a key to would be skipped silently
    inputs = action_inputs()
    env = step_env("analyze")
    for provider in KEYED_PROVIDERS:
        assert f"{provider}-api-key" in inputs
        assert env[f"{provider.upper()}_API_KEY"] == f"${{{{ inputs.{provider}-api-key }}}}"


def test_the_provider_input_names_every_provider():
    text = ACTION.read_text(encoding="utf-8")
    description = re.search(r"^  llm-provider:\n    description: (.+)$", text, re.MULTILINE)
    assert description is not None
    for provider in DEFAULT_MODELS:
        assert provider in description.group(1)


def test_the_readme_documents_every_key_input():
    for readme in READMES:
        text = readme.read_text(encoding="utf-8")
        for provider in KEYED_PROVIDERS:
            assert f"`{provider}-api-key`" in text, readme


def test_this_repositorys_pr_analysis_reviews_with_nvidia():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^ +llm-provider: nvidia$", workflow, flags=re.MULTILINE)
    assert "nvidia-api-key: ${{ secrets.NVIDIA_API_KEY }}" in workflow
    # nothing left over from the OpenAI setup: no key, and no openai extra to install
    assert "openai" not in workflow.lower()
    assert re.search(r"^ +extras: security$", workflow, flags=re.MULTILINE)


def workflow_lines() -> list[str]:
    """The workflow's YAML without its comments."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return [line.split(" #", 1)[0].rstrip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_the_key_never_reaches_code_a_pull_request_can_change():
    # On pull_request, the workflow, action.yml and pr-analyzer all come from the PR's merge
    # commit, so anyone who can push a branch here could change the code that gets the key.
    # On pull_request_target they come from the base branch, as long as the job never checks out
    # the PR.
    lines = workflow_lines()
    trigger = lines[lines.index("on:") + 1]
    assert trigger == "  pull_request_target:"
    assert "  pull_request:" not in lines
    yaml = "\n".join(lines)
    assert "uses: ./" in yaml                              # main's action, from the base checkout
    assert "ref:" not in yaml                              # so the checkout is the base branch
    assert "github.event.pull_request.head.sha" not in yaml
    assert "github.event.pull_request.head.ref" not in yaml
    assert "persist-credentials: false" in yaml
    # forks are still skipped, and the key is an environment secret, limited to main there
    assert "if: github.event.pull_request.head.repo.full_name == github.repository" in yaml
    assert "    environment: ai-review" in lines
