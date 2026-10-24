PR Analyzer

CI

An AI-powered pull request analysis tool that combines static analysis, security scanning, and LLM review into a single CLI command. Point it at any GitHub PR URL and it returns a quality score, a letter grade, inline code suggestions, and an optional GitHub review comment — all in under 30 seconds.

The Problem

Code review is slow, inconsistent, and easy to skip under deadline pressure. Junior developers miss security patterns. Senior developers miss them too when they are tired. PR Analyzer runs automatically on every pull request, catches hardcoded secrets, unsafe dependencies, high-complexity functions, and common OWASP vulnerabilities before a human ever opens the diff.

Architecture
GitHub PR URL
       │
       ▼
  GitHub Client          fetches PR metadata, diff, and raw file content
       │
       ▼
  Static Analyzer        regex patterns on diff + tree-sitter complexity on full file
       │
       ▼
  Security Scanner       secrets, dangerous code, Semgrep OWASP, pip-audit CVEs
       │
       ▼
  LLM Engine             sends PR context to OpenAI / Anthropic / Ollama
       │                 receives JSON suggestions back
       ▼
  Scorer                 groups by dimension, applies severity penalties,
       │                 computes weighted score, maps to letter grade
       ▼
  Reporter               Rich terminal table / JSON stdout / Markdown /
                         GitHub inline review comments
Technology Choices
Library	Why
PyGithub	Official GitHub REST API wrapper — handles auth, pagination, and rate limits
tree-sitter	Multi-language AST parser — counts decision nodes for cyclomatic complexity without executing code
Rich	Terminal output with colour, tables, and panels — no external dependencies
Typer	CLI framework built on Click — automatic --help, type validation, and exit codes
Pydantic Settings	Loads .env into typed config — fails fast on missing required values
structlog	Structured logging — every log line is a dict, easy to pipe to a log aggregator
Quickstart
bash
# 1. clone
git clone https://github.com/Quantumstringfloops/PRanalyzer.git
cd PRanalyzer

# 2. install
pip install poetry
poetry install

# 3. configure
cp .env.example .env
# edit .env and add your tokens

# 4. run
poetry run pr-analyzer analyze https://github.com/owner/repo/pull/123
Sample Output
╭─ PR #4821 — Fix authentication bypass ──────────────────╮
│ Author: neil        Repo: acme/api      Branch: fix → main │
│ Files: 6  +142 -38                                          │
╰──────────────────────────────────────────────────────────╯

╭─ Quality Score ──────────────────────────────────────────╮
│ Score: 61.0   Grade: D   ✗ BLOCKED                         │
│ 2 critical issue(s) found — review required                │
╰──────────────────────────────────────────────────────────╯

Dimension Scores
┌───────────────┬───────┬──────────────────────┐
│ Dimension      │ Score │ Bar                   │
├───────────────┼───────┼──────────────────────┤
│ security       │  20.0 │ ████░░░░░░░░░░░░░░░░ │
│ complexity     │ 100.0 │ ████████████████████ │
│ style          │  97.0 │ ████████████████████ │
│ documentation  │ 100.0 │ ████████████████████ │
│ testing        │ 100.0 │ ████████████████████ │
└───────────────┴───────┴──────────────────────┘

✗ BLOCKED   4 suggestion(s)   duration 8.31s
Configuration Reference
Variable	Required	Default	Description
GITHUB_TOKEN	✅	—	GitHub personal access token
OPENAI_API_KEY	✴️	—	Required if LLM_PROVIDER=openai
ANTHROPIC_API_KEY	✴️	—	Required if LLM_PROVIDER=anthropic
LLM_PROVIDER	❌	openai	openai / anthropic / ollama
LLM_MODEL	❌	gpt-4o-mini	Model name for the chosen provider
LLM_CONFIDENCE_THRESHOLD	❌	0.5	Drop LLM suggestions below this score
MAX_DIFF_LINES	❌	3000	Truncate diff before sending to LLM
Suggestion Schema
Field	Type	Allowed Values
filename	str	repo-relative path
line	int | None	line number in new file, or None
severity	str	CRITICAL HIGH MEDIUM LOW
category	str	security complexity style performance documentation testing
title	str	short label
description	str	full explanation
source	str	static security llm
confidence	float	0.0 – 1.0
Scoring Formula

Each dimension starts at 100. Every suggestion subtracts:

penalty = SEVERITY_PENALTIES[severity] × confidence

CRITICAL = 40   HIGH = 20   MEDIUM = 8   LOW = 3

Dimension score is clamped to a minimum of 0.

Overall score is a weighted sum:

score = security×0.35 + complexity×0.25 + style×0.15 + documentation×0.15 + testing×0.10
Grade Boundaries
Score	Grade
97–100	A+
93–96	A
90–92	A-
87–89	B+
83–86	B
80–82	B-
77–79	C+
73–76	C
70–72	C-
65–69	D
0–64	F

A PR is blocked if any suggestion has severity CRITICAL or HIGH, regardless of overall score.

Troubleshooting

zsh: command not found: pr-analyzer Run through Poetry: poetry run pr-analyzer or activate the shell first with poetry shell.

ModuleNotFoundError: No module named 'pr_analyzer' Run poetry install from the project root. Make sure src/pr_analyzer/__init__.py exists.

pydantic.errors.PydanticImportError: BaseSettings has been moved Your config.py imports from pydantic instead of pydantic_settings. Fix: from pydantic_settings import BaseSettings.

ValidationError: github_token field required Your .env file is missing or in the wrong directory. It must be in the project root next to pyproject.toml.

Bot posts no inline comments See the workflow section. Most common cause: the suggestion line is outside the changed chunk in the diff, so GitHub rejects the position silently.

Known Limitations
Only scans changed lines — security issues in unchanged code are not reported. A vulnerability introduced in a previous commit is invisible unless that line is touched again.
LLM suggestions are probabilistic — the confidence filter reduces noise but does not eliminate it. Always verify HIGH findings manually.
No support for monorepos — if a PR touches files across multiple packages the scorer treats them as one codebase.
tree-sitter complexity is per-file — cyclomatic complexity is counted for the whole file, not just the changed function.
Fork PRs cannot post reviews — GitHub blocks write tokens on PRs from forks by default.
Planned Improvements
Semgrep integration — run OWASP ruleset on full file content, not just changed lines
pip-audit integration — flag newly added vulnerable dependencies from requirements diffs
Auto-refactoring suggestions — generate a ready-to-apply patch for style and complexity findings
Base branch delta scoring — compare score against the base branch to show whether the PR improved or degraded quality
Web dashboard — timeline of PR scores per repository