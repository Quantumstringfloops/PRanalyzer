# Production Build Progress

_Last updated: 2026-11-03_

## Gate Decision

**Verdict: NOT VIABLE. The build is stopped.** Per the gate rule, no production code, payments, auth or pushes.

**What the application does today.** pr-analyzer is a command-line tool that one developer runs against a single GitHub pull request, on their own machine or as a step in their own CI. It uses that developer's own GitHub token, plus their own OpenAI/Anthropic key or a local Ollama model if they want the LLM review. It runs pattern and complexity checks, secret scanning, Semgrep and pip-audit, then prints a 0–100 score and letter grade. It exits with code 1 when the PR should be blocked. It is a single-user developer tool and learning project: there is no server, no database and no stored user data.

**Why a scalable, paid, multi-user product is not a sensible next step:**

1. **Accounts and payments would have nothing to protect.** There is no hosted service and no stored data, and each user runs the tool locally with their own credentials. Signup, billing and per-user data isolation would guard nothing. A license check in public source code can simply be deleted.
2. **A paid version would be a different product.** Paid tools in this space usually run as a hosted GitHub App: GitHub notifies the service about each PR, a queue hands it to a sandboxed worker, results are stored per organization, and people sign in with GitHub. The current code would only be the analysis engine inside such a worker.
3. **The engine did not work reliably** when the gate was checked: the CLI couldn't parse its flags, the LLM review never added a finding, and ordinary PRs crashed it. Those bugs are fixed on `fix/working-cli` (see below), but that branch is a working free tool, not a product.
4. **These must be solved before hosting anyone else's code:**
   - *Running untrusted code:* a shared service would need a sandbox with no secrets for every job.
   - *Customer source code and personal data:* sending private code to OpenAI or Anthropic requires customer consent, a data processing agreement, a retention and deletion policy, a privacy policy (GDPR/CCPA) and usually a security review.
   - *Payments:* Stripe Checkout keeps card data off our servers, but a business entity, terms of service, a refund policy and tax/VAT handling are still needed.
   - *Secrets:* real OpenAI and Gemini keys are exposed in this public repo's git history (commits `fb176e6`, `a895406`, `9ca208e`) and must be revoked first.

Permission to rewrite modules and the added scalability requirements don't change this. The blocker is the use case and its preconditions, not code quality.

## What would need to change for this to become viable

1. **Revoke and rotate** the leaked OpenAI and Gemini keys. Only the user can do this.
2. **Make the CLI work as a free tool.** **Done** on the local branch `fix/working-cli`. Still left: publishing to PyPI and/or as a GitHub Action.
3. **Check demand:** get real teams running it in CI. Measure the false-alarm rate and what they'd pay for that GitHub secret scanning, Dependabot, CodeQL and the open-source PR-Agent don't already provide.
4. **Only then design the hosted product:** a GitHub App, a job queue, sandboxed workers with no secrets, data stored per organization, sign-in with GitHub, Stripe subscriptions that handle failed and cancelled payments, rate limits, audit logs, and the legal and privacy work above. Re-run this gate at that point.

## Work Completed

- 2026-11-02/03: Full codebase analysis. 137 findings were confirmed across 9 areas of the code.
- 2026-11-03: Gate check: **not viable** (recorded above). No production code, payments, auth or pushes.
- 2026-11-03: **Free-CLI track, approved by the user, is complete** on the branch `fix/working-cli`, in 11 commits from `39f72d2` to `1f8877e`.
  - **Dependencies:** Typer is upgraded to 0.27 (the CLI couldn't parse its flags), the lock file is regenerated, and the semgrep/pip-audit/openai/anthropic extras are set up.
  - **Config, logging and errors:** unknown `.env` keys are ignored, logs go to stderr, and the error types are clearer.
  - **GitHub client:** long diffs, files over 1 MB and errors no longer crash it, and review posting works.
  - **Static analyzer:** line numbers are correct, the `.js`/`MEDUIM` crashes are gone, complexity is measured per function, and false alarms are cut.
  - **Security:** Semgrep runs once over every file with the right severities. pip-audit sees only exact pins, so packages chosen by a PR author are no longer downloaded or built. Regex line numbers are correct and false alarms are cut.
  - **LLM:** the Ollama variable-shadowing bug and the prompt/parser schema mismatch are fixed, every file's diff is sent, and failures become warnings.
  - **Pipeline:** `--skip-llm` and `--post-review` are wired up, failing stages degrade instead of aborting, and the scorer and reporter are hardened.
  - **CLI:** `--no-fail-on-block` exists, and the exit codes are 0 passed, 1 blocked, 2 error.
  - **Types:** mypy reports 0 errors (it was 46).
  - **CI:** the workflows moved to the repo root and now run in `pr-analyzer/`. README and `.gitignore` updated.
- **Verification:**
  - In a fresh environment built from the lock: `poetry check --lock` passes, ruff and mypy are clean, and 199 tests pass (53 tests existed before, 36 of them failing).
  - With core dependencies only: 195 pass and 4 skip (OpenAI/Anthropic SDKs absent).
  - Real unauthenticated runs on pallets/flask PRs #6162 and #6176 worked. Semgrep and Ollama warnings degraded gracefully, the JSON output was clean, and the line numbers matched the real file. Missing-PR and bad-URL runs exit 2.
- 2026-11-03: **Branch pushed** to origin after the user chose to create the pull request. The pull request is not open yet.
  - Opening it runs the PR Analysis workflow for the first time. Running the analyzer's static and security stages on the branch's own diff predicts **44.0, F, blocked, with 237 inline review comments**.
  - 164 of those comments are "Function missing return type hint" on test functions.
  - All 6 CRITICAL and 11 HIGH findings are deliberate fake secrets and unsafe-code samples in the scanner's own tests.
- 2026-11-03: **PR bot quieted**, as the user chose, in commits `18386e1`, `78ec0be`, `f87299c` and `10f1e5b`:
  - the return-type rule skips test files;
  - inline comments go only on MEDIUM and above, and LOW findings are counted in the review body;
  - a new `pr-analyzer: ignore` line marker is applied to the 40 deliberate sample lines in the tests.
  - The same local preview now predicts **passed, 67.8 (D), 7 inline comments**.
- 2026-11-03: A PR for the branch, #2 (default title "Fix/working cli"), was opened on GitHub and closed 12 seconds later, unmerged. Both workflows then ran on the old head `33f705a`:
  - The bot's job exited 1 (blocked) and left no review.
  - CI's test step failed because Typer adds colour codes on GitHub Actions. That is fixed in `ada3362`.
  - Locally, 211 tests pass, including from a clean export with Actions' environment variables.
- 2026-11-03: **PR #3 opened**, not as a draft: https://github.com/Quantumstringfloops/PRanalyzer/pull/3 (head `b657413`).
  - It was created through the browser pane after the user signed in to GitHub there.
  - **All checks passed:** CI on Python 3.11 and 3.12, and the PR Analysis bot.
  - The bot's review matched the local preview exactly: 67.8, D, passed, with 7 inline comments. That was also the first real run of `--post-review` against GitHub. The LLM step was skipped because the repo has no `OPENAI_API_KEY` secret.
  - The app can't bind the PR. `gh` 2.103.3 is installed, but the user skipped signing in. `origin` still uses the old repo name `PRlearn`; changing it was blocked by the permission check.
- 2026-11-03: **Re-runs update the bot's review instead of adding one** (`57eef90`):
  - The summary review now carries a hidden tag, and later runs edit it in place.
  - Inline comments are posted only for findings not already commented on the PR. Comments GitHub has marked outdated don't count.
  - Tests: 217 pass, including 6 new ones for repeat runs; ruff and mypy are clean.
  - **Verified on PR #3**, and all checks passed both times:
    - The run on `57eef90` added one tagged summary review, as expected, since the first review predates the tag. It repeated none of the 7 inline comments.
    - The run on `5a8c06d` added no review. It only edited the tagged one: its footer's run time went from 23.11s to 28.22s.
- 2026-11-03: **Workflow actions pinned by commit** (`d7d5f9e`), at the user's request:
  - `actions/checkout` v7.0.1 and `actions/setup-python` v7.1.0. Both run on Node 24, and nothing in their release notes affects these workflows.
  - With real Semgrep, the local bot preview now predicts 79.0 (C+), passed, with 3 inline comments. The 4 Semgrep notes about movable tags are gone.
  - On PR #3 (`5318f56`), CI on Python 3.11 and 3.12 and the bot all passed, and GitHub no longer warns about Node 20. The bot edited its tagged summary to 79.0 (C+), and its 4 Semgrep comments now show as outdated.
- 2026-11-03: **PR #3's description updated**, at the user's request, through the browser pane. It now covers the review fix, the pinned actions, 217 tests and the bot's actual result.
- 2026-11-03: PR #3 was **merged by the user** (`3d567ef`). The user also revoked both leaked keys.
- 2026-11-03: **Phase 1 (release and usability) and Phase 2 (scale and speed)**, at the user's request. Each phase was built by its own agent in its own worktree. The two were combined on `feat/release-and-scale` and merged to `main` as PR #4 (`e7e06c5`; the user merged it).
  - Phase 1 delivered:
    - PyPI packaging (MIT) and a release workflow with Trusted Publishing;
    - a composite GitHub Action, which the repo's PR bot now uses;
    - `.pr-analyzer.toml` project config with rule ids;
    - a per-line lexer: precision on a labelled sample rose from about 9% to 73–85%;
    - neutralised bot text and a fenced LLM prompt.
  - Phase 2 delivered:
    - parallel GitHub fetching: 17.5 s → 4.1 s on a real 27-file PR;
    - capped rate-limit waits;
    - chunked LLM review within a token budget;
    - an on-disk cache: 0.8 s and 3 requests on a repeat run.
  - On PR #4, CI and the Action's first real run passed, at 95.8 (A).
- 2026-11-03: **Review, then fixes.**
  - Two 10-agent review workflows, with every finding checked by a skeptic, confirmed 24 defects (11 in Phase 1, 13 in Phase 2).
  - The worst: PyPI would reject the name `pranalyzer` as a copy of `pr-analyzer`.
  - The user chose `pr-analyzer-cli`, checked against all 930,395 PyPI names with PyPI's similarity rule.
  - A fix workflow fixed all 24 in six groups, each fix checked by a verifier, and the six branches were merged onto `fix/review-findings` with no conflicts.
- 2026-11-03: **Final testing on `fix/review-findings`:**
  - **Tests:** 732 pass, and 727 pass with 5 skipped on core only; the same under GitHub Actions' variables. ruff, mypy and `poetry check --lock` are clean.
  - **Package:** the built wheel installs in a fresh venv as `pr-analyzer-cli` 0.1.0, MIT.
  - **Real runs:** a live public PR passes; JSON output is valid; a bad URL and a missing PR both exit 2.
  - **Bot preview:** 94.5 (A), passed. Its one Semgrep note was a false positive on the chmod that makes the cache folder private, now marked.
  - **README:** a step-by-step Getting Started, with an install from GitHub that works before the first PyPI release.
- 2026-11-03: **Publishing setup:** the user added the PyPI pending publisher for `pr-analyzer-cli`, and a fresh GitHub `pypi` environment was created. `gh` is logged in as Quantumstringfloops.
- 2026-11-03: **PR #5 merged** (`3244eb9`) once CI and the bot passed; the user asked for the merge to be done for them. All 24 fixes were verified, and the verifiers logged 10 new low-severity edge cases for a follow-up.
- 2026-11-03: **Demo for LinkedIn.**
  - Demo PR #6 held deliberately flawed code. The bot gave it 59.4 (F), BLOCKED, with 11 inline comments, and the PR was closed unmerged afterwards.
  - A 57-second 1080p MP4, rendered from real captures, and the LinkedIn post are in `linkedin-demo/`, which git ignores.
- 2026-11-03: **Released `pr-analyzer-cli` 0.1.0 to PyPI** from tag `v0.1.0`, on the user's go, with provenance attestations.
  - The first two publish attempts failed with `invalid-publisher`. The pending publisher's repository was `PRAnalyzer`, but GitHub's claim is `PRanalyzer`, and PyPI compares the name exactly. After the user re-added it, the third attempt published.
  - Checked with `pip install "pr-analyzer-cli[security]"` in a fresh venv: version 0.1.0, and a live public PR passes.

## Work Remaining

- **Production build:** none. The gate stopped it.
- **Free-CLI track:**
  - a 0.1.1 follow-up for the 10 low-severity edge cases the fix verifiers logged, for example:
    - a crafted `.pr-analyzer.toml` in a PR crashing the run;
    - a lexer nesting guard hiding an `eval`;
    - comments posted by the old bot version being reposted once;
    - Ctrl-C on Windows;
  - optionally purge the dead keys from git history: a force-push, only with explicit approval.
- **User actions:**
  - update the local dev venv (`poetry install --all-extras` in `pr-analyzer/`);
  - post the LinkedIn video and text.

## Next Step

**Do not start or resume a production build from this file.** The gate failed.

The free tool is released: `pip install pr-analyzer-cli`. If the user asks, work through the 0.1.1 follow-ups in a new branch and PR, then release with the next tag.

If the user changes the preconditions above and explicitly asks, re-run the gate.
