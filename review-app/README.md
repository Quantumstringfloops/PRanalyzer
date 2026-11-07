# review-app

A Qodo-style pull-request reviewer that runs on your machine. It reviews every pull request in
the repositories a GitHub App is installed on, grades it with [pr-analyzer](../pr-analyzer)
(rules, tree-sitter, Semgrep, pip-audit, and an AI review on NVIDIA-hosted models), and decides
what happens next:

- **merge**: the grade and confidence clear the repository's thresholds, and the analysis was
  complete;
- **needs review**: a person should look. The app lists why;
- **block**: blocking findings, or a grade under the block threshold.

The app is **recommend-only by default**. It reports its decision as a check run and merges
nothing until a repository opts in with `mode = "auto"`. Even then it never merges:
- when the analysis was incomplete (a scanner missing, the AI review failed, files skipped);
- a PR from a fork;
- through anything but GitHub's merge API, pinned to the analysed commit, so branch protection
  and required checks still apply.

[DESIGN.md](DESIGN.md) explains how it works and why.

## Install

You need Python 3.11 or newer. From this folder:

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -e '../pr-analyzer[security]' -e '.[dev]'
```

This installs pr-analyzer from this repository, not from PyPI: the NVIDIA provider isn't in a
release yet. The `[security]` extra brings Semgrep and pip-audit. Without them the app still
runs, but merges nothing.

```bash
cp .env.example .env
```

```bash
cp review-app.example.toml review-app.toml
```

Fill in `.env`: at least `NVIDIA_API_KEY` (create one at [build.nvidia.com](https://build.nvidia.com)).

## Try it on one PR

You don't need a GitHub App for this. Put a `GITHUB_TOKEN` in `.env`, then:

```bash
.venv/bin/review-app analyze https://github.com/owner/repo/pull/123
```

It runs the whole pipeline, analysis → decision, and prints the decision with its reasons. It
never merges, posts nothing unless you pass `--post-review`, and records everything in
`review-app.db`. Look at the result in the dashboard:

```bash
.venv/bin/review-app dashboard
```

Then open http://localhost:8001/.

## Run it for real: a GitHub App

1. **Create the app** at GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.
   - Webhook URL: a public URL that forwards to `http://127.0.0.1:8000/webhook`. For local use,
     open a channel at https://smee.io and use its URL.
   - Webhook secret: a long random string, e.g. from `openssl rand -hex 32`. It goes in
     `GITHUB_WEBHOOK_SECRET`.
   - Repository permissions: Contents *read*, Pull requests *read & write*, Checks *read &
     write*, Commit statuses *read*, Metadata *read*.
   - Subscribe to events: Pull request, Check suite, Status.
2. **Generate a private key** on the app's page. Save the `.pem` outside the repository, and
   set `GITHUB_APP_PRIVATE_KEY_PATH` to it and `GITHUB_APP_ID` to the app's id.
3. **Install the app** on the repositories it should review.
4. **Start the webhook server.** It runs the worker too:

   ```bash
   .venv/bin/review-app webhook
   ```

5. **Forward GitHub's deliveries** to it, here with the smee client:

   ```bash
   npx smee-client --url https://smee.io/<your-channel> --target http://127.0.0.1:8000/webhook
   ```

6. **Watch it** in the dashboard (`review-app dashboard`, http://localhost:8001/).

Every PR that is opened, pushed to, reopened or marked ready gets analysed. Its decision shows up
as a `review-app` check run on the commit. To let the app merge in a repository, set
`mode = "auto"` in that repository's table in `review-app.toml`. If you also make the
`review-app` check required in branch protection, its *block* result stops a merge by anyone.

Only the webhook port should ever be reachable from outside. The dashboard has no login. It
listens on loopback only and answers only to `localhost` addresses.

## Commands

| Command | What it does |
|---|---|
| `review-app webhook [--port 8000] [--no-worker]` | Receive webhooks, and work through the jobs they queue |
| `review-app worker` | The worker alone, beside `webhook --no-worker` |
| `review-app dashboard [--port 8001]` | Read-only dashboard on localhost |
| `review-app analyze <PR URL> [--post-review]` | Analyse one PR with `GITHUB_TOKEN`, recommend-only |
| `review-app check-config` | Check `review-app.toml` and the environment, and show each repository's policy |
| `review-app audit-verify` | Check the audit log's hash chain |

## Configuration

**`.env`.** Secrets and paths: see [.env.example](.env.example). pr-analyzer reads its own
settings from the same file (`LLM_MODEL`, `SEMGREP_CONFIG`, `LLM_TOKEN_BUDGET`, ...). The app
uses `LLM_PROVIDER=nvidia` unless you set another one.

**`review-app.toml`.** Policy: a `[defaults]` table, plus one table per repository that differs.
See [review-app.example.toml](review-app.example.toml). Unknown keys and inconsistent thresholds
are errors, and the app won't start on an invalid file. `mode = "auto"` is only accepted in a
repository's own table. A repository's `.pr-analyzer.toml` still shapes the analysis
(`block_on`, `disable`, `ignore_paths`).

## Tests

```bash
.venv/bin/python -m pytest
```

pr-analyzer's own suite runs from `../pr-analyzer`:

```bash
cd ../pr-analyzer && PATH="$PWD/../review-app/.venv/bin:$PATH" python -m pytest
```
