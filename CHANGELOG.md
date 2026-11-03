# Changelog

Notable changes to `pr-analyzer-cli`. The project follows [semantic versioning](https://semver.org/).

## 0.1.1 — 2026-11-03

### Fixed

**Robustness**
- A crafted `.pr-analyzer.toml` (a huge integer, or very deep nesting) is now reported as a configuration problem. It used to crash the run with "Unexpected error".
- `ignore_paths` rejects patterns containing `//`, which didn't match the way git matches them.
- Comments posted by 0.1.0 count as already posted, so the bot doesn't post them again after an upgrade.

**Security checks**
- `eval` after deeply nested f-strings or template literals is reported again.
- `TODO` and `FIXME` used as names, such as `Status.TODO` or `TODO = ...`, are no longer reported as TODO comments.

**Hardening**
- The GitHub Action removes the job's `PYTHON*` variables before installing and running pr-analyzer. A `PYTHONPATH` that pointed into a checked-out PR could otherwise reach pip, pr-analyzer, Semgrep and pip-audit.
- After the cache folder is made private, entries that were planted while others could write to it are discarded, and links are never followed.

**Windows**
- Ctrl-C now stops promptly during rate-limit waits, parallel file fetching and parallel LLM requests. Every wait is sliced into half-second steps.

### Tests
- New tests cover the code detector's safety rules, the subscript search bound, and strings that end in a backslash.

## 0.1.0 — 2026-11-03

The first release on PyPI.

### Added
- `pr-analyzer analyze <PR URL>`, which runs:
  - static checks: per-line rules and per-function cyclomatic complexity;
  - security scanning: secrets, risky calls, Semgrep's OWASP rules, and pip-audit on newly pinned dependencies;
  - an optional AI review through Ollama, OpenAI or Anthropic.

  It gives a 0–100 score and a letter grade, and blocks on critical or high findings.
- Output as a Rich terminal report, JSON or Markdown. The exit code is 0 for passed, 1 for blocked and 2 for an error.
- `--post-review`: a GitHub review with inline comments. Later runs update it in place.
- A reusable GitHub Action, `Quantumstringfloops/PRanalyzer@<ref>`.
- `.pr-analyzer.toml` project settings, read from the PR's base branch: rules to turn off, `ignore_paths`, thresholds, `block_on` and inline-comment severity.
- A `pr-analyzer: ignore` marker that drops findings on its line. Every ignored line is listed in the report.
- Parallel GitHub fetching, waits for rate limits, LLM review of big diffs in parts within a token budget, and an on-disk cache.
