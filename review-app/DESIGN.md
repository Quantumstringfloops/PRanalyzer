# review-app design

A Qodo-style pull-request reviewer that runs on your own machine. It reviews every pull request
in the repositories a GitHub App is installed on, grades it with pr-analyzer, and decides one
of three things: **merge**, **needs review** (send it to a human) or **block**.

Merging is fail-safe. By default the app only recommends. It merges for real only in a
repository that opts into it, only through GitHub's merge API, and never when the analysis was
incomplete or the PR comes from a fork.

## Goals and non-goals

Goals:
- Reuse pr-analyzer as the analysis engine. The app adds no analysis of its own.
- Run the AI review on NVIDIA-hosted models: the `nvidia` LLM provider in pr-analyzer, using
  `NVIDIA_API_KEY`.
- Record every decision and every action in an append-only audit log.
- Run locally with a few documented commands, using SQLite and no other services.

Non-goals for now:
- User accounts, login, payments. The only credentials are the GitHub App's: its private key,
  its webhook secret and the installation tokens minted from them. The dashboard is read-only
  and bound to loopback, and anything more needs the user's decision first.
- Running untrusted PR code. pr-analyzer only reads diffs and file contents. Semgrep scans
  copies of the files, and pip-audit runs with `--no-deps --disable-pip`.

## Components

```
 GitHub ──webhook──▶ webhook server ──▶ jobs table ──▶ worker ──▶ pr-analyzer (AnalysisPipeline)
   ▲   (HMAC-checked)     │  (SQLite)                 │            └─ NVIDIA NIM for the AI review
   │                      ▼                           ├─▶ decision engine (pure function)
   │                 audit log ◀──────────────────────┤
   └──── check run / merge API / review requests ◀────┘  action executor (re-checks before acting)

                    dashboard (read-only, 127.0.0.1) ──reads──▶ SQLite
```

| Module | Job |
|---|---|
| `settings.py` | Environment settings: the GitHub App id and key, the webhook secret, file paths |
| `policy.py` | Per-repository policy from `review-app.toml`: mode, thresholds, merge method, reviewers |
| `db.py` | SQLite schema and data access, including the hash-chained audit log |
| `jobs.py` | Durable job queue in SQLite: dedupe per PR, retries with backoff, crash recovery |
| `webhooks.py` | Signature check (`X-Hub-Signature-256`, constant-time) and event routing |
| `models.py` | The facts passed between parts: a PR's state, its files, an analysis outcome |
| `analysis.py` | Runs pr-analyzer with an installation token, collects what the decision needs |
| `decision.py` | Completeness, confidence and the decision itself, as pure functions |
| `github_app.py` | App JWT → installation tokens (cached), plus a small gateway for PR state, check runs, merges and review requests |
| `actions.py` | Carries out a decision: check run, review request, merge (with the fail-safe gates) |
| `worker.py` | Claims jobs and runs analyze → decide → act |
| `web.py` | Two FastAPI apps: the webhook receiver and the read-only dashboard |
| `cli.py` | `review-app webhook / dashboard / worker / analyze / check-config / audit-verify` |

The webhook server and the dashboard are separate apps on separate ports. Only the webhook
port is meant to be forwarded from the internet (smee.io, a tunnel). The dashboard holds no
secrets, opens the database read-only, refuses to bind to anything but loopback, and accepts
only `localhost` / `127.0.0.1` Host headers, against DNS rebinding.

## Flow

1. **Webhook.** The receiver verifies the HMAC over the raw body. It rejects a missing or bad
   signature (401), a body over 5 MiB (413), or any request at all when no secret is set (503).
   It drops repeated deliveries by `X-GitHub-Delivery`.
2. **Queue.** `pull_request` `opened`, `reopened`, `synchronize` and `ready_for_review` (and
   `edited` when the base branch changed) enqueue an `analyze` job for that head SHA. A newer
   push supersedes queued jobs for the same PR. `closed` cancels them. Installation events keep
   the repository list current. Completed check suites and statuses re-queue a merge that was
   waiting for required checks.
3. **Analyze.** The worker re-reads the PR. A closed PR is skipped. If the head has moved on,
   the job is superseded by one for the new head. Otherwise it runs `AnalysisPipeline` with an
   installation token. pr-analyzer posts its usual review comments when the repository's policy
   has `post_review`.
4. **Decide.** The decision is a pure function of the analysis, the PR's metadata and the
   repository's policy (below).
5. **Act.** The executor reports the decision as a check run on the analysed commit and acts on
   it according to the repository's mode. Every step lands in the audit log.

## Decision engine

Rules apply in order:

1. **Block** if pr-analyzer found blocking issues (its `passed` is false: a finding whose
   severity is in the project's `block_on`, CRITICAL and HIGH by default), or if the score or
   grade is under the policy's `block_below_score` or `block_below_grade`. Blocking wins even
   over an incomplete analysis, since a fuller analysis can only find more.
2. **Needs review** if any merge gate fails. Every failing gate is listed as a reason:
   - the analysis is **incomplete** (see below), or it failed outright;
   - the PR isn't open (`review-app analyze` reports on closed PRs too);
   - the PR comes from a **fork** (head repo ≠ base repo, or the head repo is gone);
   - the PR is a draft, or targets a branch outside `base_branches` (default: the repository's
     default branch);
   - it changes a path in `review_paths` (default: `.github/**`, `.pr-analyzer.toml`,
     `CODEOWNERS`);
   - the AI review did not run while `require_llm_review` is on (the default);
   - score < `merge_min_score`, grade < `merge_min_grade`, or confidence <
     `merge_min_confidence`.
3. **Merge** otherwise.

**Incomplete analysis.** pr-analyzer reports skipped work as free-text warnings. The app treats
**every** warning as incompleteness, except "Could not post the GitHub review", which is about
reporting, not analysis. So wording changes, and warnings added in later versions, fail safe.
That covers scanners that were skipped or failed, an AI review that failed or covered only some
files, files skipped by `ignore_paths`, an invalid `.pr-analyzer.toml`, and findings silenced
with `pr-analyzer: ignore` (a PR could silence its own findings). The app also checks things
pr-analyzer doesn't warn about:
- changed files that never reached the analysis (no patch: binary files, huge diffs, renames);
- files whose full content couldn't be fetched;
- semgrep or pip-audit missing from the app's environment;
- Semgrep, the dependency audit or the AI review switched off in the project's
  `.pr-analyzer.toml`;
- an AI reply that couldn't be read, or was cut off at `LLM_MAX_TOKENS`. These become warnings
  in pr-analyzer itself.

**Confidence** (0–1) measures how far a *complete* analysis can be trusted. It is the product of
three factors, each shown on the dashboard:
- **size**: 1.0 up to 400 changed lines, falling linearly to 0.5 at 2,000 or more (review quality
  drops on large changes);
- **coverage**: 0.7 + 0.3 × the share of changed lines in files pr-analyzer parses fully
  (Python, JavaScript, TypeScript) or in docs;
- **AI certainty**: 1.0 − 0.1 for each MEDIUM-or-worse AI finding with confidence under 0.8,
  never below 0.6.

## Fail-safe merging

- **Recommend-only by default.** `mode = "recommend"` posts the check run and records what it
  would have done. `mode = "auto"` must be set per repository; `[defaults]` refuses it.
- **Gates checked twice.** The decision applies them, and the executor re-reads the PR right
  before merging and checks again: open, not a draft, not from a fork, an allowed base branch,
  head still at the analysed SHA.
- **Merging only through GitHub's merge API** (`PUT /pulls/{n}/merge`), pinned with
  `sha=<analysed head>`. Branch protection, required reviews and required checks still apply.
  A refusal is recorded, never worked around. A merge refused only because required checks are
  pending is retried when a check suite or status completes, at most 5 times per commit.
- **Fail closed everywhere.** An invalid `review-app.toml` stops the app. A failed analysis
  means needs review. Errors from GitHub are recorded and retried, and never turn into a merge.

## Data

SQLite in WAL mode. Tables: `repositories`, `pull_requests`, `analyses` (score, grade,
findings, warnings), `decisions` (decision, confidence, reasons, the policy and facts used, the
outcome), `jobs`, `deliveries`, and `audit_log`. Triggers make the audit log append-only. Each
row also stores the SHA-256 of the previous row's hash plus its own content, so
`review-app audit-verify` can detect edits made outside the app.

## Configuration

- **Secrets** come from the environment or `.env`: `GITHUB_APP_ID`,
  `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET` and `NVIDIA_API_KEY`. pr-analyzer reads
  its own settings (`LLM_MODEL`, `SEMGREP_CONFIG` and so on) from the same place.
  `LLM_PROVIDER` defaults to `nvidia` in the app.
- **Policy** lives in `review-app.toml`: a `[defaults]` table plus `[repos."owner/name"]`
  overrides. Unknown keys are errors, so a typo can't silently loosen a gate. The file belongs
  to the operator, not to the repositories, so a PR can't change its own policy.
- **GitHub App permissions:** Contents read, Pull requests write (reviews, merges, review
  requests), Checks write, Commit statuses read, Metadata read. Events: Pull request, Check
  suite, Status, Installation.

## Open questions for the user

- Should files skipped by `ignore_paths` keep blocking auto-merge? (They do now: it's the
  strict reading of "files skipped".)
- User login for the dashboard, if it ever needs to leave localhost.
