# Production Build Progress

_Last updated: 2026-11-03_

## Gate Decision

**Verdict: NOT VIABLE. The build is stopped.** Per the gate rule, no production code, payments, auth or pushes.

**What the application does today.** pr-analyzer is a command-line tool that one developer runs against a single GitHub pull request, on their own machine or as a step in their own CI. It uses that developer's own GitHub token, plus their own OpenAI/Anthropic key or a local Ollama model if they want the LLM review. It runs pattern and complexity checks, secret scanning, Semgrep and pip-audit, then prints a 0–100 score and letter grade. It exits with code 1 when the PR should be blocked. It is a single-user developer tool and learning project: there is no server, no database and no stored user data.

**Why a scalable, paid, multi-user product is not a sensible next step:**

1. **Accounts and payments would have nothing to protect.** There is no hosted service and no stored data, and each user runs the tool locally with their own credentials. Signup, billing and per-user data isolation would guard nothing. A license check in public source code can simply be deleted.
2. **A paid version would be a different product.** Paid tools in this space usually run as a hosted GitHub App: GitHub notifies the service about each PR, a queue hands it to a sandboxed worker, results are stored per organization, and people sign in with GitHub. The current code would only be the analysis engine inside such a worker.
3. **The engine does not work reliably yet** (each point verified in this session):
   - with the locked dependencies, the CLI cannot parse its own flags;
   - the LLM review never adds a finding;
   - ordinary PRs crash it: any `.js`/`.jsx` file, a deleted last file, a file with complexity over 20, or a diff over 3000 lines;
   - its false alarms would block customers' PRs.
4. **These must be solved before hosting anyone else's code:**
   - *Running untrusted code:* `pip-audit -r` builds packages named in the PR. A shared service would need a sandbox with no secrets for every job.
   - *Customer source code and personal data:* sending private code to OpenAI or Anthropic requires customer consent, a data processing agreement, a retention and deletion policy, a privacy policy (GDPR/CCPA) and usually a security review.
   - *Payments:* Stripe Checkout keeps card data off our servers, but a business entity, terms of service, a refund policy and tax/VAT handling are still needed.
   - *Secrets:* real OpenAI and Gemini keys are exposed in this public repo's git history (commits `fb176e6`, `a895406`, `9ca208e`) and must be revoked first.

Permission to rewrite modules and the added scalability requirements don't change this. The blocker is the use case and its preconditions, not code quality.

## What would need to change for this to become viable

1. **Revoke and rotate** the leaked OpenAI and Gemini keys.
2. **Make the CLI work as a free tool:**
   - fix the verified crashes and the features that silently do nothing;
   - upgrade Typer (0.27.3 parses every flag correctly with any Click version) and regenerate the lock file;
   - move CI to the repo root and get the tests passing;
   - cut the false alarms;
   - publish to PyPI and/or as a GitHub Action.
3. **Check demand:** get real teams running it in CI. Measure the false-alarm rate and what they'd pay for that GitHub secret scanning, Dependabot, CodeQL and the open-source PR-Agent don't already provide.
4. **Only then design the hosted product:** a GitHub App, a job queue, sandboxed workers with no secrets, data stored per organization, sign-in with GitHub, Stripe subscriptions that handle failed and cancelled payments, rate limits, audit logs, and the legal and privacy work above. Re-run this gate at that point.

## Work Completed

- 2026-11-02/03: Full codebase analysis. 137 findings were confirmed across 9 areas of the code. Tests: 43 pass and 10 fail with Semgrep available (it was faked for the run).
- 2026-11-03: Gate check: **not viable** (recorded above). No production code, payments, auth or pushes.
- A separate track that the user approved: fixing the CLI as a free tool on the local branch `fix/working-cli`. The branch was created and the toolchain checked, then the work paused before any code edits when the production-build prompt arrived. No fix commits exist and nothing has been pushed.

## Work Remaining

- **Production build:** none. The gate stopped it.
- **Free-CLI track** (paused; needs the user's go-ahead):
  - packaging and the Typer upgrade;
  - the GitHub client;
  - diff line mapping and the static analyzer;
  - the security scanners;
  - the LLM engine;
  - the pipeline, scorer, reporter and logging;
  - tests;
  - CI and docs;
  - verifying everything in a clean environment.

## Next Step

**Do not start or resume a production build from this file.** The gate failed. A resumed session must not write production code, add payments or auth, or push anything. Wait for the user to do one of these:

- (a) resume the free-CLI fix on `fix/working-cli`;
- (b) change the preconditions above and explicitly ask for the gate to be re-run.
