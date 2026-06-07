# Security Policy

## Threat model

`cmd-overwatch` is a **single-operator, localhost-only** tool. The server binds to
`127.0.0.1` and the configuration loader refuses any other host at startup. There is
no authentication — the design assumption is "click-as-operator": whoever sits at the
keyboard is the operator.

Consequence: **anyone who can reach the port can POST events and read the buffered
tool-call stream.** On a typical developer workstation with no port forwarding this
means only processes running as you. If you run overwatch on a shared machine, behind
a tunnel, or in any configuration where another user can reach `localhost:8765`, you
must add authentication (e.g. a reverse proxy with a shared bearer token) before use.

**Do not change `server.host` to `0.0.0.0` or expose the port to a network.**

---

## What the dashboard exposes

The Live feed contains every tool call that Claude Code makes: file paths, command
lines, Bash arguments, file contents passed to Write/Edit, and full tool responses.
The Issues, Approvals, and AAR tabs contain derived data from those events: task
titles, error excerpts, solution plans, and the history of Approve/Deny decisions.

All of this is scoped to Claude Code activity on this machine. Nothing is sent
anywhere unless you enable a notification channel.

---

## Redaction guarantees

Every event payload and every executor transcript is passed through `overwatch/redact.py`
before being written to the SQLite database. The following patterns are scrubbed and
replaced with `[REDACTED:<label>]`:

| Label | Pattern |
|-------|---------|
| `private-key` | PEM private key blocks (`-----BEGIN ... PRIVATE KEY-----`) |
| `anthropic-key` | `sk-ant-` API keys |
| `openai-key` | `sk-` / `sk-proj-` API keys |
| `aws-access-key` | `AKIA` / `ASIA` access key IDs |
| `github-token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` tokens |
| `github-pat` | `github_pat_` personal access tokens |
| `slack-token` | `xox[baprs]-` tokens |
| `discord-webhook` | Discord webhook URLs |
| `jwt` | JSON Web Tokens (`eyJ…`) |
| `bearer` | `Bearer <token>` header values |
| `assignment` | `password=`, `token=`, `api_key=`, `secret=`, and similar key=value pairs |

Redaction happens at the storage boundary (`Store.add_event`, `Store.upsert_issue`,
`Store.add_solution`). Runtime data (`data/overwatch.db`, `data/transcripts/`) is
gitignored and never leaves the machine. Notification payloads are built from
already-redacted database rows and re-redacted again before send.

---

## Executor safety rails

The headless executor (`overwatch/solution/executor.py`) runs `claude -p` under strict
constraints. All of the following are enforced in code, not config:

1. **DRY_RUN default** — `dry_run = true` in `config.example.toml` and in the code
   default. Until you set `dry_run = false` explicitly in your `config.toml`, no
   subprocess is ever spawned, no files are deleted, and no notifications are sent. In
   dry-run mode the planned command is written to a transcript and nothing else happens.

2. **Approval gate** — gated solutions never execute without a recorded `approved`
   decision in the database. The executor re-checks authorization immediately before
   spawning; a denial between dispatch and execution is respected.

3. **Branch isolation** — every execution works in a fresh `git worktree` on a
   `fix/<id>-<slug>` branch. The operator's working directory is never touched. The
   branch names `main`, `master`, and `production` are refused at the executor level.

4. **Restricted tool allowlist** — the `claude -p` invocation passes `--allowedTools`
   (Read, Edit, Write, Glob, Grep, limited Bash) and `--disallowedTools` (WebFetch,
   WebSearch, git push, git merge, gh, curl, wget, rm). The agent cannot widen its own
   permissions.

5. **Subprocess timeout + kill** — the subprocess is given a hard timeout
   (default 600 s). On expiry it is killed, the transcript records the timeout, and the
   issue transitions to `failed`.

6. **Single in-flight lock** — a PID lock file under `data/` ensures only one
   execution runs at a time across the whole machine.

7. **Never merges** — the executor's ceiling is "prepare a fix branch." Merging into
   any branch is always a human step.

8. **Transcript capture and redaction** — stdout, stderr, branch name, and exit code
   are redacted and written to `data/transcripts/` before any status update reaches
   the database.

---

## Gate categories

These built-in categories are **immutable and code-enforced**. Operator config can add
extra patterns via `[gates] extra_patterns` but cannot remove or narrow any built-in
category.

| Category | What triggers it |
|----------|-----------------|
| `money` | Payment, invoice, billing, subscription, pricing, refund, payout |
| `publishing` | Publish, release, deploy; making something public; social/advertising/newsletter |
| `customer-data` | Customer/client data, records, messages; PII, personal data, GDPR |
| `secrets` | Secret, credential, API key, token, password, passphrase; `.env`; private key |
| `main-merge` | Merge/push/commit to main/master/production; force push |
| `destructive` | Delete/drop/remove/purge/prune a branch, tag, database, backup, snapshot, repo; `rm -rf`; `filter-repo` |
| `auth-network` | Auth/authn/authz/authenticate/authorize; firewall, open port, expose to network; `0.0.0.0`; permission widen |
| `service-install` | Install/register/enable a service, scheduled task, daemon, or startup item |
| `legal` | Terms of service, privacy policy, license change, compliance, warranty |

Anything that does not match a gate pattern but whose `kind` is not on the explicit
auto-allowlist (`log-purge`, `task-restart`, `report-only`) is classified as `uncertain`
and also gated.

---

## Notification channel security

Notifications are off by default (`discord = false`, `email = false` in `config.toml`).
When enabled:

- The Discord webhook URL is read from the `OVERWATCH_DISCORD_WEBHOOK` environment
  variable at send time. It is never written to `config.toml` or the database.
- The SMTP password is read from `OVERWATCH_SMTP_PASSWORD`. All other SMTP settings
  (host, port, user, addresses) may be in `config.toml`, but are non-secret.
- Payloads are built from already-redacted database data and passed through `redact_text`
  again immediately before the outbound request.
- With `dry_run = true`, the full payload is logged to stdout but no request is made.

---

## Not a multi-user tool

`cmd-overwatch` has no user accounts, no CSRF protection, no rate limiting, and no
session audit log for the dashboard UI. Approval decisions are recorded in the database
with a `decided_by` field (currently always `"dashboard"`), but UI sessions are not
tracked. This is by design for a single-operator local tool. Do not deploy it as a
shared service.

---

## Reporting security issues

Please report security vulnerabilities via **GitHub Issues** using the private
vulnerability reporting feature, or by email to the maintainer listed in the repository.
Include:

- Steps to reproduce
- The version (or commit hash) you are running
- The potential impact as you see it

Allow up to **90 days** for a fix before public disclosure. We will acknowledge receipt
within 7 days and aim to provide a fix or workaround within 30 days for critical issues.
