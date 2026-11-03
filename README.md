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
              └────────┬────────┘  (8 files at a time, from the on-disk cache where it can)
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
              │   LLM Engine    │  sends the line-numbered diff to Ollama / OpenAI / Anthropic
              └────────┬────────┘  (a big one in parts); receives JSON suggestions back
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

Every check reports only lines the PR adds, so a PR is never blamed for code it didn't touch: complexity is reported for the functions the PR changes, and file length only when the PR takes a file past the limit. The optional stages degrade instead of failing: if Semgrep or pip-audit isn't installed, or the LLM can't be reached, the run carries on and the report lists a warning.

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

## Install

Python 3.11 or newer. The package is called `pranalyzer` on PyPI; the command it installs is `pr-analyzer`.

```bash
pip install pranalyzer                  # static checks, secret scanning, LLM review through Ollama
pip install "pranalyzer[security]"      # adds Semgrep and pip-audit
pip install "pranalyzer[openai]"        # adds the OpenAI SDK, for LLM_PROVIDER=openai
pip install "pranalyzer[anthropic]"     # adds the Anthropic SDK, for LLM_PROVIDER=anthropic
```

Extras combine: `pip install "pranalyzer[security,openai]"`. `pipx install pranalyzer` works too.

## Quickstart

```bash
export GITHUB_TOKEN=ghp_...      # optional for public repos; needed for private ones and --post-review
pr-analyzer analyze https://github.com/owner/repo/pull/123
```

The default LLM is a local Ollama model (`ollama pull llama3.2`). If Ollama isn't running, the review still completes without LLM suggestions and says so; use `--skip-llm` to skip it on purpose, or set `LLM_PROVIDER` to `openai` or `anthropic` (install the matching extra).

### From source (contributors)

```bash
git clone https://github.com/Quantumstringfloops/PRanalyzer.git
cd PRanalyzer/pr-analyzer
pip install "poetry>=2.2"
poetry install --all-extras
cp .env.example .env            # then fill in GITHUB_TOKEN and the LLM settings
poetry run pr-analyzer analyze https://github.com/owner/repo/pull/123
poetry run pytest
```

---

## Usage

```
pr-analyzer analyze <PR URL> [OPTIONS]
pr-analyzer version
```

| Option | Default | Description |
|--------|---------|-------------|
| `--skip-llm` | off | Run the static and security checks only |
| `--post-review` | off | Post the result as a GitHub review: inline comments for MEDIUM and above (configurable), LOW findings counted in the summary. Later runs on the same PR update that summary and comment only on new findings (needs a token that can write pull requests) |
| `--fail-on-block` / `--no-fail-on-block` | on | Exit with code 1 when the PR is blocked |
| `--output`, `-o` | `rich` | `rich`, `json` or `markdown`. Reports go to stdout, logs to stderr |
| `--config PATH` | — | Project settings to use instead of the `.pr-analyzer.toml` on the PR's base branch (see [Project configuration](#project-configuration-pr-analyzertoml)) |
| `--verbose`, `-v` | off | Show progress logs |
| `--no-cache` | off | Don't read or write the [on-disk cache](#caching) for this run |

URLs copied from a PR's *Files changed*, *Commits* or *Checks* tab work too.

**Exit codes:** `0` passed · `1` blocked (with `--fail-on-block`) · `2` the PR could not be analysed (bad URL, authentication, rate limit, invalid configuration, or an unexpected error)

### Ignoring a finding

Put `pr-analyzer: ignore` in a comment on the line, ideally with the reason:

```python
subprocess.run(cmd, shell=True)  # pr-analyzer: ignore - cmd is a fixed string
```

Every finding on that line is dropped and doesn't count towards the score, whichever check reported it: the regex rules, Semgrep, the dependency audit or the LLM. The report's warnings name each ignored line, so a reviewer can see what a PR silenced.

### Text the bot posts

Finding titles and descriptions can come from the LLM, which reads the PR, from Semgrep messages that quote the PR's code, and from file names the PR chose. Before posting a review the bot neutralises that text with an invisible zero-width space, so it reads the same but can't @mention anyone, add HTML (such as an `<!-- -->` comment that hides the rest of the review or fakes the bot's hidden tag), show links or images, or cross-reference issues; file names are shown as code.

The LLM prompt treats the PR the same way: its title, file names and diff sit between `<untrusted_pr_content>` tags that the model is told hold code to review and never instructions, any copy of the tags inside the PR is escaped, and the scanner findings the model must not repeat sit in a block of their own. Prompt injection can't be ruled out entirely, so the LLM's reply is still checked strictly: only JSON suggestions about files in the PR, with known severities and categories, are kept.

### What the line rules skip

The pattern rules read one added line at a time and tell code, string literals and comments apart in Python, JavaScript and TypeScript (and `//`, `/* */` and `"..."` in C-family languages such as Java, Go or Rust):

- Rules about calls and settings (`eval(`, `pickle.load`, `yaml.load`, `subprocess … shell=True`, `debug=True`, `print(`, `console.log`) ignore text inside strings and comments. The SQL-injection rule still sees a string being formatted into a query.
- `todo-comment` counts `TODO` and `FIXME` only in comments.
- The secret rules read the whole line, strings and comments included, since that is where secrets end up.
- `magic-number` skips numbers that are already named or explain themselves: a keyword argument or default value (`timeout=30`), an assignment (`retries = 5`), a dict key or object property (`"port": 8080`, `{ port: 8080 }`), a constant definition (`MAX_SIZE = 10`), a slice or index (`line[:72]`), a line about a version (`sys.version_info >= (3, 11)`), and well-known values: 10, 100 and 1000; 24, 60, 3600 and 86400; powers of two from 16 to 65536, 255 and 65535; ports 80 and 443; and the standard HTTP status codes. Test files are skipped.

Because each line is read on its own, the middle lines of a multi-line string (a docstring, a template literal) or of a `/* */` comment are read as code unless they start like a comment (`*`, `#`, `//`); code inside f-string braces or `${...}` counts as part of the string; and JavaScript regex literals aren't recognised. Other languages are matched as before, anywhere in the line.

### Project configuration (`.pr-analyzer.toml`)

A repository can tune the checks with a `.pr-analyzer.toml` file in its root. Every key is optional:

```toml
# rule ids and whole stages to switch off (see the table below)
disable = ["magic-number", "missing-return-type"]

# files to leave out: no findings are reported for them and they are not sent to the LLM
ignore_paths = ["docs/**", "*.min.js", "tests/fixtures/"]

# severities that block the PR (exit code 1 with --fail-on-block); [] never blocks
block_on = ["CRITICAL", "HIGH"]

# the lowest severity that gets an inline review comment; lower ones are counted in the summary
inline_min_severity = "MEDIUM"

[thresholds]
complexity_medium = 15   # a function's cyclomatic complexity above this is MEDIUM...
complexity_high = 30     # ...and above this, HIGH
max_file_lines = 500     # a PR that takes a file past this gets a file-length finding
max_line_length = 120    # longer added lines get a long-line finding
```

The values shown are the defaults. Severities can be written in any case.

**Where it is read from.** When a PR is analysed, `.pr-analyzer.toml` is read from the PR's **base branch** through the GitHub API, never from the PR itself, so a PR can't loosen its own checks; a change to the file takes effect once it is merged. A file in the current directory is never picked up (in CI that is the PR's own checkout). `--config PATH` uses a local file instead. No file means the defaults.

**Validation.** Unknown keys, unknown rule ids, bad severities, unsupported `ignore_paths` patterns and thresholds that aren't positive whole numbers make the file invalid. Every problem is named, with a suggestion for likely typos, and what happens next depends on where the file came from:

- **An invalid file on the base branch** doesn't stop the run. The file is ignored, the PR is analysed with the default settings, and a warning names the problems in the report, in the review's notes and on stderr:

  ```
  .pr-analyzer.toml on main is invalid, so the default settings were used: disable: unknown rule id 'magic-numbers' (did you mean 'magic-number'?)
  ```

  To recover, fix the file in a PR to that branch. That PR is analysed with the defaults like any other; once it is merged, the project's settings apply again.
- **An invalid `--config` file** stops the run with exit code 2 before anything is fetched:

  ```
  Configuration error: settings.toml is invalid:
    - unknown key 'disabel' (did you mean 'disable'?)
    - disable: unknown rule id 'magic-numbers' (did you mean 'magic-number'?)
  ```

**`ignore_paths` patterns** use a subset of `.gitignore` syntax, matched the way git matches it:

- `*` matches any run of characters within one path segment, and `?` any one character except `/`.
- `**` as a whole path segment crosses directories: `**/` at the start or `/**/` in the middle stands for any number of directories, including none, and `/**` at the end for everything inside. Within a name, `**` is the same as `*`, so `**.min.js` means `*.min.js`.
- A pattern with no `/` except at the end (like `*.min.js` or `vendor/`) matches in every directory; one with a leading or inner `/` (like `/setup.py` or `docs/**`) only from the repository root.
- A trailing `/` matches directories only, and a pattern that matches a directory covers every file in it.
- Every other character matches itself, case-sensitively, `#` and spaces included (a `.gitignore` would read a leading `#` as a comment and drop trailing spaces).

Negation (a leading `!`), `[` and `]` (character classes), backslashes (escapes) and `**` inside a name before a `/` (like `foo**/bar`, which git itself reads inconsistently) make the file invalid, rather than silently matching something other than what a `.gitignore` would. The report notes which files were skipped.

**Rule ids.** Each finding's id is in the JSON report (`"rule"`), and the review summary shows it next to each counted rule.

| Id | Flags | Severity | Applies to |
|----|-------|----------|------------|
| `todo-comment` | `TODO` / `FIXME` in a comment | LOW | source files |
| `console-log` | `console.log(` | LOW | JavaScript / TypeScript |
| `print-call` | `print(` | LOW | Python |
| `bare-except` | `except:` | MEDIUM | Python |
| `long-line` | added line longer than `max_line_length` | LOW | source files |
| `magic-number` | unexplained number literal | LOW | source files, not tests |
| `missing-return-type` | `def` without `->` | LOW | Python, not tests |
| `complexity` | function above `complexity_medium` / `complexity_high` | MEDIUM / HIGH | Python, JavaScript, TypeScript |
| `file-length` | a file the PR takes past `max_file_lines` | LOW | Python, JavaScript, TypeScript |
| `aws-access-key` | AWS access key id | CRITICAL | every file |
| `hardcoded-api-key` | `api_key = "..."` and similar | CRITICAL | every file |
| `private-key` | PEM private key block | CRITICAL | every file |
| `github-token` | GitHub token | CRITICAL | every file |
| `jwt-token` | JSON Web Token | HIGH | every file |
| `hardcoded-password` | `password = "..."` and similar | CRITICAL | every file |
| `db-connection-string` | database URL with a password | CRITICAL | every file |
| `sql-injection` | SQL built with an f-string, `%`, `.format()` or `+` | HIGH | source files |
| `eval` | `eval(` | HIGH | source files |
| `pickle-load` | `pickle.load(` / `pickle.loads(` | HIGH | source files |
| `yaml-load` | `yaml.load(` without `SafeLoader` | HIGH | source files |
| `subprocess-shell` | `subprocess.*(..., shell=True)` | HIGH | source files |
| `insecure-random` | `random` used for tokens, passwords or keys | MEDIUM | source files |
| `debug-mode` | `debug=True` | MEDIUM | source files |
| `semgrep` | the whole Semgrep scan | per Semgrep rule | changed files |
| `dependency-audit` | the whole pip-audit check | HIGH | `requirements*.txt` |
| `llm` | the whole LLM review | per suggestion | the diff |

To tune individual Semgrep rules, point `SEMGREP_CONFIG` at your own ruleset.

---

## Use it in GitHub Actions

Add a workflow to the repository you want reviewed:

```yaml
# .github/workflows/pr-analyzer.yml
name: PR Analyzer

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions: {}

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      contents: read          # read the PR's files and the base branch's .pr-analyzer.toml
      pull-requests: write    # post the review
    steps:
      - id: pr
        uses: Quantumstringfloops/PRanalyzer@<commit-sha>   # pin a commit, as for any action
        with:
          llm-provider: openai                              # optional: leave out to skip the LLM
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          extras: security,openai
```

No checkout step is needed: the action reads the PR through the GitHub API and installs pr-analyzer from its own copy of this repository, so the action and the code always match.

| Input | Default | Description |
|-------|---------|-------------|
| `github-token` | `github.token` | Reads the PR and posts the review |
| `pr-url` | the PR that triggered the run | The pull request to analyse |
| `llm-provider` | empty | `openai`, `anthropic` or `ollama`. Empty skips the LLM review; a provider without its key is skipped with a note in the review |
| `llm-model` | the provider's default | Model to use |
| `openai-api-key`, `anthropic-api-key` | empty | Pass a secret here |
| `post-review` | `true` | Post the result as a review |
| `fail-on-block` | `true` | Fail the step when the PR is blocked |
| `config` | empty | A settings file in the workspace, used instead of the base branch's `.pr-analyzer.toml` (see below) |
| `extras` | `security` | Comma-separated optional parts: `security`, `openai`, `anthropic` |
| `python-version` | `3.12` | Python to run pr-analyzer with |

**Outputs:** `score` (0–100), `grade` (`A+` to `F`) and `passed` (`true`/`false`), read from the JSON report, e.g. `${{ steps.pr.outputs.grade }}`.

- The action runs on Linux and macOS runners.
- On `pull_request`, PRs from forks get a read-only token and no secrets, so the review can't be posted; skip them with `if: github.event.pull_request.head.repo.full_name == github.repository` on the job, as this repository's own workflow does.
- With `llm-provider: ollama`, set `OLLAMA_URL` in the step's `env:`.
- `config` reads a file from the workspace. If the job checks out the PR, that is the PR's own code, which could loosen its own checks, so prefer the base branch's `.pr-analyzer.toml` unless the file comes from somewhere the PR can't change.
- pr-analyzer runs outside the workspace, so a `.env` file in the PR can't change its settings, and the log can't be fed workflow commands by text from the PR.

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
| `MAX_DIFF_LINES` | ❌ | `3000` | Diff lines per LLM request; a bigger diff is [reviewed in parts](#big-pull-requests) |
| `LLM_TOKEN_BUDGET` | ❌ | `200000` | Estimated prompt tokens (characters ÷ 4) one run may send; files past it aren't AI-reviewed, and the report says how many. `0` = no limit |
| `LLM_MAX_CONCURRENCY` | ❌ | `1` | LLM requests in flight at once when a PR is reviewed in parts |
| `SEMGREP_CONFIG` | ❌ | `p/owasp-top-ten` | Semgrep ruleset: registry name, URL or local path |
| `SEMGREP_TIMEOUT_SECONDS` | ❌ | `300` | Time limit for the Semgrep run |
| `PIP_AUDIT_TIMEOUT_SECONDS` | ❌ | `300` | Time limit for pip-audit |
| `GITHUB_TIMEOUT_SECONDS` | ❌ | `30` | Time limit for each GitHub API request |
| `GITHUB_MAX_WORKERS` | ❌ | `8` | Files whose content is fetched at the same time |
| `GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS` | ❌ | `60` | Longest wait for a GitHub rate limit to reset; a longer one fails the run (exit code 2). `0` = never wait |
| `CACHE_ENABLED` | ❌ | `true` | `false` turns the [on-disk cache](#caching) off |
| `CACHE_DIR` | ❌ | `$XDG_CACHE_HOME/pr-analyzer`, else `~/.cache/pr-analyzer` | Where the cache lives |
| `CACHE_MAX_SIZE_MB` | ❌ | `500` | Beyond this, the least recently used entries are removed |
| `CACHE_MAX_AGE_DAYS` | ❌ | `30` | Entries unused for this long are removed |
| `LOG_LEVEL` | ❌ | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (`--verbose` sets `INFO`) |

---

## Big Pull Requests

**Fetching.** The full content of `GITHUB_MAX_WORKERS` files is fetched at a time, each worker thread with a GitHub client of its own. With 100 ms per request, a 50-file PR took 19.2 s before and takes 3.1 s now (`benchmarks/fetch_benchmark.py`).

**Rate limits.** When GitHub answers with a rate limit (403 or 429), the request is sent again once the limit allows: after `Retry-After` seconds, at `X-RateLimit-Reset` for the hourly limit, or after a minute when GitHub doesn't say. Waits are logged, and the other requests hold back meanwhile. A wait longer than `GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS` isn't sat out: the run stops with exit code 2 and says when to try again.

**LLM review in parts.** A diff of up to `MAX_DIFF_LINES` lines is reviewed in one request, as before. A bigger one is split into parts of whole files (a file too big for one part is cut between hunks, or between lines, keeping its line numbers), each reviewed in its own request, and the suggestions are merged. `LLM_TOKEN_BUDGET` caps what one run sends: once the next part would go over it, the remaining parts are skipped and a warning says how many files weren't AI-reviewed. If a request fails, the parts already reviewed are kept and the warning says what was left out. Parts are sent one at a time by default, because Ollama may queue parallel requests (see its `OLLAMA_NUM_PARALLEL`) and time spent in its queue counts towards `LLM_TIMEOUT_SECONDS`; for OpenAI and Anthropic, `LLM_MAX_CONCURRENCY=2` to `4` reviews a big PR faster.

### Caching

Results that can't change are kept on disk, in `CACHE_DIR`:

- **file contents**, by git blob SHA: a file that didn't change keeps its blob SHA from push to push, so a new push fetches only the files it changed;
- **LLM replies**, by provider, model and the exact prompt: re-running the same diff costs no tokens and doesn't count towards `LLM_TOKEN_BUDGET`.

Failed requests are never cached, and neither is anything that contains your API keys or looks like a token or private key. A damaged entry counts as a miss, entries are written atomically, and the size and age limits are applied at most once an hour. The directory is private to your user, since it may hold code from private repositories. `--no-cache` skips the cache for one run (for a fresh LLM review, say); `CACHE_ENABLED=false` turns it off.

To keep the cache between GitHub Actions runs, restore and save it with `actions/cache`. A saved cache can't be updated, so each run saves a new one and the next run restores the newest:

```yaml
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pr-analyzer
          key: pr-analyzer-${{ runner.os }}-${{ github.event.pull_request.number }}-${{ github.run_id }}
          restore-keys: |
            pr-analyzer-${{ runner.os }}-${{ github.event.pull_request.number }}-
            pr-analyzer-${{ runner.os }}-
```

GitHub scopes caches by branch, so a PR's runs restore what earlier runs of the same PR saved (or the default branch's), never another PR's.

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

A PR is **blocked** if any suggestion has severity `CRITICAL` or `HIGH`, regardless of overall score. A project can choose other severities with `block_on` in `.pr-analyzer.toml`.

---

## Troubleshooting

**`zsh: command not found: pr-analyzer`**
Activate the virtualenv you installed `pranalyzer` into, or use `pipx install pranalyzer`. From a source checkout, run it through Poetry: `poetry run pr-analyzer`.

**`ModuleNotFoundError: No module named 'pr_analyzer'`**
Install the package (`pip install pranalyzer`), or from a source checkout run `poetry install` in the `pr-analyzer/` directory (the one with `pyproject.toml`).

**`Authentication failed` or `GitHub rate limit hit`**
Set `GITHUB_TOKEN`. The `.env` file must be in the directory you run the command from, or export the variable in your shell. A rate limit that resets within `GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS` is waited out; the message means it resets later than that.

**Warning: `… file(s) were not AI-reviewed, or only in part`**
The PR was reviewed in parts and some were left out: either `LLM_TOKEN_BUDGET` ran out (raise it, or set `0` for no limit) or an LLM request failed (the warning names the error). The other checks covered every file.

**Warning: `Semgrep scan skipped` / `Dependency audit skipped`**
The scanners are optional. Install them with `pip install "pranalyzer[security]"` (from source: `poetry install --extras "security"`).

**Warning: `LLM review skipped: Could not reach Ollama`**
Start Ollama and pull the model (`ollama pull llama3.2`), point `LLM_PROVIDER` at OpenAI or Anthropic, or pass `--skip-llm`.

**Bot posts no inline comments**
Only MEDIUM, HIGH and CRITICAL findings get inline comments (or the severities from `inline_min_severity` up), and only on lines that are part of the diff. Other findings at those levels are listed in the review body, and LOW findings are counted there by rule. A finding that already has a comment isn't commented on again when the PR is re-analysed. If GitHub rejects the inline comments, the summary is still posted. PRs from forks can't be reviewed from Actions (read-only token, no secrets).

---

## Known Limitations

- **Only scans changed lines** — security issues in unchanged code are not reported. A vulnerability introduced in a previous commit is invisible unless that line is touched again.
- **LLM suggestions are probabilistic** — the confidence filter reduces noise but does not eliminate it. Always verify HIGH findings manually.
- **No support for monorepos** — if a PR touches files across multiple packages the scorer treats them as one codebase.
- **Complexity covers Python, JavaScript and TypeScript only** — other languages get the regex checks but no complexity score.
- **Dependency audit checks exact pins only** — `name==version` lines added to `requirements*.txt`; ranges, `pyproject.toml` and lock files are not audited.
- **Semgrep's default ruleset is downloaded** from the Semgrep registry on every run, so it needs network access (point `SEMGREP_CONFIG` at a local file to avoid that).
- **Big PRs are reviewed in parts** — the LLM sees one part of the diff at a time, so it can miss a problem that only shows across files in different parts.
- **Fork PRs cannot post reviews** — GitHub blocks write tokens on PRs from forks by default.
- **Ignore markers are the PR author's call** — anyone can add `pr-analyzer: ignore` to a line. The report names every ignored line, so check them during review.
- **Line rules read one line at a time** — text in the middle of a multi-line docstring or comment can still trigger a code rule (see [What the line rules skip](#what-the-line-rules-skip)).
- **Secret rules don't know test fixtures** — a placeholder password in a test (`password="pass"`) is reported like a real one; mark it with `pr-analyzer: ignore` or leave the fixtures out with `ignore_paths`.

---

## Planned Improvements

- **Auto-refactoring suggestions** — generate a ready-to-apply patch for style and complexity findings
- **Base branch delta scoring** — compare score against the base branch to show whether the PR improved or degraded quality
- **Dependency audit for `pyproject.toml` and lock files**
- **Web dashboard** — timeline of PR scores per repository

---

## Releasing

Maintainers publish to PyPI by pushing a version tag; `.github/workflows/release.yml` does the rest:

1. Set the new `version` in `pr-analyzer/pyproject.toml` and merge it to `main`.
2. Tag that commit and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.

The workflow stops if the tag doesn't match the version, then runs the tests, builds the wheel and sdist, checks them with `twine check --strict`, installs the wheel and runs it, and publishes with PyPI Trusted Publishing, so no API token is stored anywhere. Running the workflow by hand (Actions → Release → Run workflow) publishes the build to TestPyPI instead, as a rehearsal; TestPyPI refuses a version it already has.

One-time setup: on PyPI, add a trusted publisher for the project `pranalyzer` with owner `Quantumstringfloops`, repository `PRanalyzer`, workflow `release.yml` and environment `pypi`; do the same on TestPyPI with environment `testpypi`. Then create both environments under the repository's *Settings → Environments*. Required reviewers on `pypi` make every release wait for an approval.

---

## License

MIT. See [LICENSE](https://github.com/Quantumstringfloops/PRanalyzer/blob/main/LICENSE).
