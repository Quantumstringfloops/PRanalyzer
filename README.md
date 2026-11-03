# PR Analyzer

[![CI](https://github.com/Quantumstringfloops/PRanalyzer/actions/workflows/ci.yml/badge.svg)](https://github.com/Quantumstringfloops/PRanalyzer/actions/workflows/ci.yml)

An AI-powered pull request analysis tool that combines static analysis, security scanning, and LLM review into a single CLI command. Point it at any GitHub PR URL and it returns a quality score, a letter grade, line-level suggestions, and an optional GitHub review with inline comments.

---

## The Problem

Code review is slow, inconsistent, and easy to skip under deadline pressure. Junior developers miss security patterns. Senior developers miss them too when they are tired. PR Analyzer runs automatically on every pull request, catches hardcoded secrets, unsafe dependencies, high-complexity functions, and common OWASP vulnerabilities before a human ever opens the diff.

---

## Architecture

```
                 GitHub PR URL
                       │
                       ▼
              ┌─────────────────┐
              │  GitHub Client  │  fetches PR metadata, each file's diff, and its full content
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Static Analyzer │  regex rules on added lines + tree-sitter complexity per function
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │Security Scanner │  secrets and risky code on added lines, Semgrep OWASP rules,
              └────────┬────────┘  pip-audit on newly pinned requirements
                       │
                       ▼
              ┌─────────────────┐
              │   LLM Engine    │  sends the line-numbered diff to Ollama / OpenAI / Anthropic;
              └────────┬────────┘  receives JSON suggestions back
                       │
                       ▼
              ┌─────────────────┐
              │     Scorer      │  groups by dimension, applies severity penalties,
              └────────┬────────┘  computes weighted score, maps to letter grade
                       │
                       ▼
              ┌─────────────────┐
              │    Reporter     │  Rich terminal table / JSON / Markdown on stdout,
              └─────────────────┘  plus a GitHub review with inline comments (--post-review)
```

Every check reports only lines the PR adds, so a PR is never blamed for code it didn't touch. The optional stages degrade instead of failing: if Semgrep or pip-audit isn't installed, or the LLM can't be reached, the run carries on and the report lists a warning.

---

## Technology Choices

| Library | Why |
|---------|-----|
| PyGithub | Official GitHub REST API wrapper — handles auth, pagination, and rate limits |
| tree-sitter | Multi-language parser — counts decision points per function for cyclomatic complexity without executing code |
| Semgrep, pip-audit | OWASP rule matching, and known-vulnerability lookups for newly pinned dependencies (the optional `security` extra) |
| Rich | Terminal output with colour, tables, and panels |
| Typer | CLI framework — automatic `--help`, type validation, and exit codes |
| Pydantic Settings | Loads `.env` and environment variables into typed config |
| structlog | Structured logging to stderr — every log line is key/value data |

---

## Quickstart

```bash
# 1. clone
git clone https://github.com/Quantumstringfloops/PRanalyzer.git
cd PRanalyzer/pr-analyzer

# 2. install (Poetry 2.x). Extras: security (Semgrep + pip-audit), openai, anthropic
pip install poetry
poetry install --extras "security"

# 3. configure (a GitHub token is optional for public repos)
cp .env.example .env
# edit .env: GITHUB_TOKEN, plus the LLM settings if you don't run Ollama locally

# 4. run
poetry run pr-analyzer analyze https://github.com/owner/repo/pull/123
```

The default LLM is a local Ollama model (`ollama pull llama3.2`). If Ollama isn't running, the review still completes without LLM suggestions and says so; use `--skip-llm` to skip it on purpose, or set `LLM_PROVIDER` to `openai` or `anthropic` (install the matching extra).

---

## Usage

```
pr-analyzer analyze <PR URL> [OPTIONS]
pr-analyzer version
```

| Option | Default | Description |
|--------|---------|-------------|
| `--skip-llm` | off | Run the static and security checks only |
| `--post-review` | off | Post the result as a GitHub review: inline comments for MEDIUM and above, LOW findings counted in the summary. Later runs on the same PR update that summary and comment only on new findings (needs a token that can write pull requests) |
| `--fail-on-block` / `--no-fail-on-block` | on | Exit with code 1 when the PR is blocked |
| `--output`, `-o` | `rich` | `rich`, `json` or `markdown`. Reports go to stdout, logs to stderr |
| `--verbose`, `-v` | off | Show progress logs |

URLs copied from a PR's *Files changed*, *Commits* or *Checks* tab work too.

**Exit codes:** `0` passed · `1` blocked (with `--fail-on-block`) · `2` the PR could not be analysed (bad URL, authentication, rate limit, or an unexpected error)

### Ignoring a finding

Put `pr-analyzer: ignore` in a comment on the line, ideally with the reason:

```python
subprocess.run(cmd, shell=True)  # pr-analyzer: ignore - cmd is a fixed string
```

Every finding on that line is dropped and doesn't count towards the score, whichever check reported it: the regex rules, Semgrep, the dependency audit or the LLM. The report's warnings name each ignored line, so a reviewer can see what a PR silenced.

---

## Sample Output

```
╭─ PR #4821 — Fix authentication bypass ──────────────────────╮
│ Author: neil                                                │
│ Repo: acme/api                                              │
│ Branch: fix → main                                          │
│ Draft: No                                                   │
│ Files: 6  +142  -38                                         │
╰─────────────────────────────────────────────────────────────╯
╭─ Quality Score ─────────────────────────────────────────────╮
│ Score: 71.5   Grade: C-   ✗ BLOCKED                         │
│ 2 critical issue(s) found — review required                 │
╰─────────────────────────────────────────────────────────────╯
               Dimension Scores
┌───────────────┬───────┬──────────────────────┐
│ Dimension     │ Score │ Bar                  │
├───────────────┼───────┼──────────────────────┤
│ security      │  20.0 │ ████░░░░░░░░░░░░░░░░ │
│ style         │  97.0 │ ███████████████████░ │
│ complexity    │ 100.0 │ ████████████████████ │
│ documentation │ 100.0 │ ████████████████████ │
│ testing       │ 100.0 │ ████████████████████ │
└───────────────┴───────┴──────────────────────┘

✗ BLOCKED  3 suggestion(s)  duration 8.31s
```

---

## Configuration Reference

Settings come from environment variables or a `.env` file in the directory you run the command from (see `.env.example`). Unrelated keys in `.env` are ignored.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | ✴️ | — | Optional for public repos (unauthenticated: 60 requests/hour). Required for private repos and `--post-review` |
| `LLM_PROVIDER` | ❌ | `ollama` | `ollama` / `openai` / `anthropic` |
| `LLM_MODEL` | ❌ | per provider | `llama3.2` / `gpt-4o-mini` / `claude-haiku-5-5` |
| `OLLAMA_URL` | ❌ | `http://localhost:11434` | Where Ollama is listening |
| `OPENAI_API_KEY` | ✴️ | — | Required if `LLM_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | ✴️ | — | Required if `LLM_PROVIDER=anthropic` |
| `LLM_CONFIDENCE_THRESHOLD` | ❌ | `0.5` | Drop LLM suggestions below this confidence |
| `LLM_TIMEOUT_SECONDS` | ❌ | `120` | Time limit for one LLM request |
| `LLM_MAX_TOKENS` | ❌ | `4096` | Response length limit (Anthropic) |
| `MAX_DIFF_LINES` | ❌ | `3000` | Diff lines sent to the LLM; the rest is left out of the prompt |
| `SEMGREP_CONFIG` | ❌ | `p/owasp-top-ten` | Semgrep ruleset: registry name, URL or local path |
| `SEMGREP_TIMEOUT_SECONDS` | ❌ | `300` | Time limit for the Semgrep run |
| `PIP_AUDIT_TIMEOUT_SECONDS` | ❌ | `300` | Time limit for pip-audit |
| `GITHUB_TIMEOUT_SECONDS` | ❌ | `30` | Time limit for each GitHub API request |
| `LOG_LEVEL` | ❌ | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (`--verbose` sets `INFO`) |

---

## Suggestion Schema

| Field | Type | Allowed Values |
|-------|------|----------------|
| `filename` | `str` | repo-relative path |
| `line` | `int \| None` | line number in the new file, or `None` |
| `severity` | `str` | `CRITICAL` `HIGH` `MEDIUM` `LOW` |
| `category` | `str` | `security` `complexity` `style` `documentation` `testing` |
| `title` | `str` | short label |
| `description` | `str` | full explanation |
| `source` | `str` | `static` `security` `llm` |
| `confidence` | `float` | `0.0` – `1.0` |

---

## Scoring Formula

Each dimension starts at 100. Every suggestion subtracts:

```
penalty = SEVERITY_PENALTIES[severity] × confidence

CRITICAL = 40   HIGH = 20   MEDIUM = 8   LOW = 3
```

Dimension score is clamped to a minimum of 0. Confidence is clamped to 0–1, and a suggestion whose category isn't one of the five dimensions counts as `style`.

Overall score is a weighted sum, rounded to one decimal before grading (so the score shown always matches the grade):

```
score = security×0.35 + complexity×0.25 + style×0.15 + documentation×0.15 + testing×0.10
```

### Grade Boundaries

| Score | Grade |
|-------|-------|
| 97–100 | A+ |
| 93–96 | A |
| 90–92 | A- |
| 87–89 | B+ |
| 83–86 | B |
| 80–82 | B- |
| 77–79 | C+ |
| 73–76 | C |
| 70–72 | C- |
| 65–69 | D |
| 0–64 | F |

A PR is **blocked** if any suggestion has severity `CRITICAL` or `HIGH`, regardless of overall score.

---

## Troubleshooting

**`zsh: command not found: pr-analyzer`**
Run through Poetry: `poetry run pr-analyzer`, or activate the virtualenv first.

**`ModuleNotFoundError: No module named 'pr_analyzer'`**
Run `poetry install` from the `pr-analyzer/` directory (the one with `pyproject.toml`).

**`Authentication failed` or `GitHub rate limit hit`**
Set `GITHUB_TOKEN`. The `.env` file must be in the directory you run the command from, or export the variable in your shell.

**Warning: `Semgrep scan skipped` / `Dependency audit skipped`**
The scanners are optional. Install them with `poetry install --extras "security"` (or `pip install semgrep pip-audit`).

**Warning: `LLM review skipped: Could not reach Ollama`**
Start Ollama and pull the model (`ollama pull llama3.2`), point `LLM_PROVIDER` at OpenAI or Anthropic, or pass `--skip-llm`.

**Bot posts no inline comments**
Only MEDIUM, HIGH and CRITICAL findings get inline comments, and only on lines that are part of the diff. Other findings at those levels are listed in the review body, and LOW findings are counted there by rule. A finding that already has a comment isn't commented on again when the PR is re-analysed. If GitHub rejects the inline comments, the summary is still posted. PRs from forks can't be reviewed from Actions (read-only token, no secrets).

---

## Known Limitations

- **Only scans changed lines** — security issues in unchanged code are not reported. A vulnerability introduced in a previous commit is invisible unless that line is touched again.
- **LLM suggestions are probabilistic** — the confidence filter reduces noise but does not eliminate it. Always verify HIGH findings manually.
- **No support for monorepos** — if a PR touches files across multiple packages the scorer treats them as one codebase.
- **Complexity covers Python, JavaScript and TypeScript only** — other languages get the regex checks but no complexity score.
- **Dependency audit checks exact pins only** — `name==version` lines added to `requirements*.txt`; ranges, `pyproject.toml` and lock files are not audited.
- **Semgrep's default ruleset is downloaded** from the Semgrep registry, so it needs network access (point `SEMGREP_CONFIG` at a local file to avoid that).
- **Fork PRs cannot post reviews** — GitHub blocks write tokens on PRs from forks by default.
- **Ignore markers are the PR author's call** — anyone can add `pr-analyzer: ignore` to a line. The report names every ignored line, so check them during review.

---

## Planned Improvements

- **Auto-refactoring suggestions** — generate a ready-to-apply patch for style and complexity findings
- **Base branch delta scoring** — compare score against the base branch to show whether the PR improved or degraded quality
- **Dependency audit for `pyproject.toml` and lock files**
- **Web dashboard** — timeline of PR scores per repository
