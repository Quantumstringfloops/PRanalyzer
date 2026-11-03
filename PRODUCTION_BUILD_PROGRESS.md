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

## Work Remaining

- **Production build:** none. The gate stopped it.
- **Free-CLI track:**
  - open the pull request (the branch is pushed; see Next Step);
  - publish to PyPI or as a GitHub Action;
  - optionally purge the leaked `.env` from git history (a force-push to a public repo; only after the keys are rotated, and only with explicit approval).
- **User actions:**
  - revoke the leaked OpenAI and Gemini keys;
  - update the local dev venv (`poetry install --extras "security"` in `pr-analyzer/`), which still has Typer 0.12.5.

## Next Step

**Do not start or resume a production build from this file.** The gate failed. `fix/working-cli` is pushed, but the pull request is not open. Wait for the user to choose between two options:

- quiet the PR bot first: skip the return-type rule in test files, post inline comments only for MEDIUM and above, and add an inline marker that suppresses deliberate test fixtures;
- open the pull request as it is.

Then open it, bind it, and record the link here. If the user changes the preconditions above and explicitly asks, re-run the gate.
