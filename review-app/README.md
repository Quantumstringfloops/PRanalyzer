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

New to it? [HOW-IT-WORKS.md](HOW-IT-WORKS.md) walks through the whole app in plain words: a
PR's journey, how decisions are made, the safety rails, the dashboard, where data lives, and
what runs today. [DESIGN.md](DESIGN.md) explains the design and why.

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

Only the webhook port should ever be reachable from outside. Without login, the dashboard
listens on loopback only and answers only to `localhost` addresses.

## Dashboard login (Supabase)

The dashboard can require a sign-in. Supabase Auth keeps the accounts and their passwords, and
review-app never stores either. Only the addresses you list get in. Once login is set up, the
dashboard may also be served beyond localhost.

1. **Create a Supabase project** at [supabase.com](https://supabase.com). Email sign-in is on by
   default (Authentication → Sign In / Providers → Email).
2. **Turn off public sign-ups** there ("Allow new users to sign up"). The dashboard has no
   sign-up page, and the allowlist below refuses everyone else anyway.
3. **Add the admins' accounts** under Authentication → Users → Add user (or Invite user).
4. **Copy the Project URL and the anon key** (also called the publishable key) from Project
   Settings → API into `.env`. Don't use the service-role key: review-app never needs it.

   ```bash
   SUPABASE_URL=https://<project>.supabase.co
   SUPABASE_ANON_KEY=<the anon / publishable key>
   REVIEW_APP_ADMIN_EMAILS=you@example.com, @your-team.example
   ```

   Projects on Supabase's JWT signing keys (the default for new ones) need nothing else.
   review-app reads the public keys from the project's JWKS. A project still on the legacy JWT
   secret also needs `SUPABASE_JWT_SECRET` (Project Settings → API → JWT Settings).
5. **Check it** with `review-app check-config`, then run `review-app dashboard`. Every page now
   asks you to sign in.

**Serving it beyond localhost.** List the host names it should answer to in
`REVIEW_APP_DASHBOARD_HOSTS` (for example `review.example.com`), and run
`review-app dashboard --host 0.0.0.0` behind HTTPS (a reverse proxy, or a tunnel that terminates
TLS). Keep the original `Host` header when proxying. Off localhost the session cookies are
`Secure`, so plain http won't keep you signed in. Without login, the dashboard refuses any address
but loopback.

**How sessions work.** Signing in stores Supabase's access token (valid for an hour by default) and
refresh token in HttpOnly, `SameSite=Lax` cookies. Every request checks the access token's
signature, issuer, audience and expiry, and that the address is still on the allowlist. An expired
token is renewed with the refresh token, and you sign in again after a week. Signing out ends the
session at Supabase. Five failed sign-ins for an address from one client lock that pair out for
15 minutes.

| Endpoint | What it does |
|---|---|
| `GET /login` | The sign-in page |
| `POST /auth/login` | Sign in: the page's form, or JSON `{"email", "password"}`. Sets the cookies; JSON clients also get the tokens back |
| `POST /auth/logout` | End the session at Supabase and clear the cookies |
| `POST /auth/refresh` | New tokens, from the refresh cookie or JSON `{"refresh_token"}` |
| `GET /auth/me` | The signed-in user, as JSON (401 when not signed in) |

The dashboard still changes nothing: these are its only POST endpoints. A POST from another site
(`Origin` set to anything but the dashboard) is refused. JSON clients can send
`Authorization: Bearer <access token>` instead of cookies.

## Commands

| Command | What it does |
|---|---|
| `review-app webhook [--port 8000] [--no-worker]` | Receive webhooks, and work through the jobs they queue |
| `review-app worker` | The worker alone, beside `webhook --no-worker` |
| `review-app dashboard [--port 8001] [--host ...]` | Read-only dashboard: on localhost, or anywhere once login is set up |
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

## Troubleshooting

**`No module named 'pr_analyzer'` (or `'review_app'`) on macOS.** Python 3.12 and later skip
`.pth` files that carry the macOS *hidden* flag, and editable installs rely on them. Files under
a hidden folder, such as a worktree in `.claude/worktrees/`, can get that flag. Clear it:

```bash
chflags nohidden .venv/lib/python3.*/site-packages/*.pth
```

## Tests

```bash
.venv/bin/python -m pytest
```

pr-analyzer's own suite runs from `../pr-analyzer`:

```bash
cd ../pr-analyzer && PATH="$PWD/../review-app/.venv/bin:$PATH" python -m pytest
```
