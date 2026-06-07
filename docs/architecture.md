# Architecture

`cmd-overwatch` is composed of three independent processes that share a single
WAL-mode SQLite database. This document describes each component in detail.

---

## Component overview

| Component | Entry point | Role |
|-----------|-------------|------|
| **Dashboard server** | `server.py` (`uvicorn`) | Accepts hook events via `POST /event`; serves the browser dashboard and REST API; broadcasts events over WebSocket |
| **Collector** | `python -m overwatch.collector` | Scheduled every 30 min; scans four signal classes; upserts issues; drafts and dispatches solutions |
| **AAR generator** | `python -m overwatch.aar` | Scheduled daily at 07:30; reads the database; writes a Markdown after-action report; delivers notifications |
| **Hook capture** | `hooks/capture.py` | Executed by Claude Code before and after every tool call; reads JSON from stdin; POSTs to `POST /event` |
| **Config loader** | `overwatch/config.py` | Loads and validates `config.toml`; enforces loopback-only host constraint; provides typed `Config` dataclass |
| **Store** | `overwatch/store.py` | Thin DAO over SQLite; all write paths apply redaction; enforces issue lifecycle transitions |
| **Redact** | `overwatch/redact.py` | Pattern-based scrubber applied at the storage boundary |
| **Gate classifier** | `overwatch/detect/gate_classifier.py` | Classifies proposed remediations as GATED or AUTO based on immutable built-in patterns plus operator-configured extras |
| **Drafter** | `overwatch/solution/drafter.py` | Produces a solution `body_md` and classification for a given issue |
| **Executor** | `overwatch/solution/executor.py` | Runs approved solutions via `claude -p` under strict rails |
| **Pipeline** | `overwatch/solution/pipeline.py` | Orchestrates draft → classify → route for each open issue after each collector pass |

---

## Data flow

### Hook event (Claude Code → Dashboard → DB → Browser)

1. The `claude` CLI fires a shell hook before and after each tool call.
2. `hooks/capture.py` reads the event JSON from stdin and `POST /event`s it to
   `http://127.0.0.1:8765/event`.
3. The FastAPI server calls `Store.add_event`, which runs the payload through
   `redact_value` and inserts the redacted JSON into the `events` table.
4. The server pushes the redacted event to all connected WebSocket clients.
5. The browser dashboard renders the event in the Live tab.

The hook is non-blocking: if the server is not running, `capture.py` exits with code 0
rather than blocking or raising.

### Issue detection (Collector → DB)

1. Windows Task Scheduler runs `python -m overwatch.collector` every 30 minutes.
2. The collector instantiates each detector in sequence:
   - `log_scan.py` — reads each target's `log_dir` for exit codes, tracebacks, ERROR lines, and stale log age
   - `sched_tasks.py` — queries Windows Task Scheduler for each `task_folders` entry
   - `git_hygiene.py` — runs `git status` / `git log` for each target's `repo`
   - `host_health.py` — checks disk usage, Windows Event Log, backup file recency
3. Each detector returns a list of `Finding` objects (fingerprint, source, severity,
   title, evidence dict).
4. `persist_findings` upserts each finding via `Store.upsert_issue`. Identical
   fingerprints bump the `count` and `last_seen` timestamp rather than creating a new
   row. A resolved issue that recurs is re-opened; a `wontfix` issue stays closed.
5. The pipeline (`solution/pipeline.py`) then drafts a solution for every `open` issue
   that has none, classifies the gate category, and either queues the issue for approval
   or dispatches it for auto-execution.

### Approval flow (Browser → DB → Executor)

1. The browser dashboard shows gated issues in the **Approvals** tab.
2. The operator clicks Approve or Deny; the browser `POST /api/approvals` with the
   solution ID and decision.
3. The server calls `Store.record_approval` (append-only; one decision per solution,
   ever).
4. For an approved solution the server calls `pipeline.dispatch_solution`, which calls
   `Executor.execute`.
5. The executor verifies authorization, creates a `git worktree` on a fresh
   `fix/<id>-<slug>` branch, and spawns `claude -p` under its restricted tool
   allowlist.
6. On completion the transcript is redacted and written to `data/transcripts/`; the
   issue status advances to `resolved` or `failed`.

---

## SQLite schema overview

Database path: `data/overwatch.db` (WAL mode, foreign keys on).

| Table | Purpose |
|-------|---------|
| `events` | Raw (redacted) hook event payloads from Claude Code. Buffered for the Live and Health tabs. Indexed by `created_at`. |
| `issues` | Detected problems. One row per unique fingerprint. Tracks `status`, `severity`, `source`, `count`, `first_seen`, `last_seen`, and `gate_category`. |
| `solutions` | Drafted remediation plans, one per issue per drafting pass. Contains `body_md`, `gate_category`, `auto_eligible`, and `kind`. |
| `approvals` | Append-only operator decision log. One row per solution, ever. `decision` is `'approved'` or `'denied'`. |
| `aar_records` | Metadata index for generated after-action reports: `report_date`, file `path`, and a short `summary`. |
| `host_health` | Time-series health readings (disk %, event log severity, backup age). Latest reading per metric is displayed in the Health tab. |
| `log_purge_runs` | Audit log of log purge operations: files deleted, bytes freed, dry-run flag. |

---

## Issue lifecycle

```
open
  │ drafted (solution written)
  ▼
drafted
  ├─► executing (auto-eligible)
  └─► pending_approval (gated)

pending_approval
  ├─► executing (operator approved)
  ├─► open (operator denied — issue stays visible)
  └─► wontfix (operator denied permanently — recurrences bump count only)

executing
  ├─► resolved (exit 0)
  └─► failed (exit non-zero, timeout, or lock contention)

failed
  └─► drafted (redraft on next collector pass)

resolved
  └─► open (signal recurs on next collector pass)

wontfix (terminal — no further transitions)
```

Fingerprint deduplication ensures that a log error that fires on every collector run
increments `count` on a single issue row rather than flooding the list.

---

## Gate classifier

`overwatch/detect/gate_classifier.py` runs on the combined text of an issue's title,
diagnosis, and proposed action.

**Decision algorithm (fails safe):**

1. If the text matches any pattern in any **built-in gate category** → GATED (that
   category).
2. Otherwise, if it matches any **operator-configured extra pattern** → GATED
   (`custom`).
3. Otherwise, if the solution `kind` is in `SAFE_KINDS`
   (`log-purge`, `task-restart`, `report-only`) → AUTO.
4. Otherwise → GATED (`uncertain`).

A match always wins; there is no way to promote a matched solution to auto.

**Built-in gate categories (immutable):**

`money`, `publishing`, `customer-data`, `secrets`, `main-merge`, `destructive`,
`auth-network`, `service-install`, `legal`.

See `SECURITY.md` for the full pattern list. Operator config (`[gates] extra_patterns`)
can add additional patterns but cannot remove or modify built-in ones.

---

## Executor rails

See `overwatch/solution/executor.py`. All constraints are code-enforced.

| Rail | Implementation |
|------|----------------|
| Approval check | `_authorize()` re-reads the `approvals` table immediately before spawning; a stale or missing approval is a refusal. |
| Dry-run | `cfg.dry_run` short-circuits before any subprocess call; planned command is logged to a transcript only. |
| Branch isolation | `git worktree add -b fix/<id>-<slug>` in `data/worktrees/issue-<id>/`; `main`, `master`, `production` refused by name. |
| Tool restriction | `--allowedTools "Read Edit Write Glob Grep Bash(python:*) Bash(python3:*)"` and `--disallowedTools "WebFetch WebSearch Bash(git push:*) Bash(git merge:*) Bash(gh:*) Bash(curl:*) Bash(wget:*) Bash(rm:*)"` passed directly to `claude -p`. |
| Timeout | `subprocess.run(..., timeout=600)` with `TimeoutExpired` caught; issue transitions to `failed`. |
| Single flight | `ExecutorLock` writes a PID lock file under `data/`; stale locks (older than 1200 s) are reclaimed. |
| Transcript + redaction | stdout, stderr, branch name, exit code written via `redact_text` to `data/transcripts/issue-<id>-<ts>.log`. |
| No merge | The executor never calls `git merge`, `git push`, or `gh pr`. The fix branch is left for human review. |
| Fail closed | Missing `claude` CLI, missing `.git` directory, or lock contention all produce `"refused"` or `"failed"` results; there is no auto-retry. |

---

## Config-driven targets

Each `[[targets]]` entry in `config.toml` drives all four detection signals for that
project:

- **Log scanning** uses `log_dir` + `log_glob` to find files, and `max_log_age_hours`
  to detect schedules that silently stopped.
- **Git hygiene** uses `repo` to run git status and log queries.
- **Task Scheduler** uses the top-level `task_folders` list (not per-target) to check
  all registered tasks within those folders.
- **Host health** runs once per collector pass regardless of targets (disk, event log,
  backup recency checks are machine-wide).

With no `config.toml` present, overwatch starts with safe defaults and an empty target
list. The collector and AAR still run but produce no findings.

---

## Redaction

`overwatch/redact.py` provides two functions:

- `redact_text(s: str) -> str` — applies the pattern list to a string, replacing each
  match with `[REDACTED:<label>]` where the label names the credential type.
- `redact_value(v: Any) -> Any` — recursively walks dicts, lists, and tuples, applying
  `redact_text` to every string value (dict keys are left intact as structural).

Called at these boundaries:

| Call site | What is redacted |
|-----------|-----------------|
| `Store.add_event` | Full event payload (tool inputs, outputs, file contents) |
| `Store.upsert_issue` | Issue title and evidence dict |
| `Store.add_solution` | Solution `body_md` |
| `Store.add_host_health` | Health metric values |
| `Store.add_aar_record` | AAR summary field |
| `Executor._write_transcript` | Full executor stdout/stderr/command log |
| Notification send paths | Payload strings immediately before outbound request |

---

## Scheduled task layout

Two tasks are registered by `scheduler/Install-Schedule.ps1`:

| Scheduler path | Trigger | Command |
|----------------|---------|---------|
| `\Overwatch\Collector` | Repeat every 30 min, indefinitely | `pwsh scheduler\run_collector.ps1` → `python -m overwatch.collector` |
| `\Overwatch\Daily AAR` | Daily at 07:30 | `pwsh scheduler\run_aar.ps1` → `python -m overwatch.aar` |

Both scripts are thin wrappers in `scheduler/` that activate the project's virtual
environment (if present), set `OVERWATCH_CONFIG` to the project root's `config.toml`,
and invoke the Python module. `scheduler/_common.ps1` contains shared path resolution
logic.

Tasks run under the current user account with "Run whether user is logged on or not"
unchecked by default (they are visible-session tasks). Adjust the task properties in
Task Scheduler if you need background S4U execution.
