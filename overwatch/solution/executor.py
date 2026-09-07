"""Headless executor: run an approved solution via ``claude -p`` under rails.

Safety rails (all enforced here, none optional) — but see the
``TODO(comprehension)`` on ``ALLOWED_TOOLS``/``DISALLOWED_TOOLS`` below, which
qualifies the "Restricted tools" rail: it is enforced here only to the extent
the ``claude`` CLI's own gating inspects Bash's argv the way this module
assumes:

- **Approval**: a gated solution executes only with a recorded ``approved``
  decision; auto solutions only when their stored draft is auto-eligible.
- **Dry-run default**: with ``dry_run`` on (the default), the exact planned
  command is recorded to the transcript and *nothing is spawned*.
- **Worktree isolation**: work happens on a fresh ``fix/<issue>-<slug>``
  branch in a dedicated ``git worktree`` under overwatch's data dir — the
  operator's checkout is untouched and the default branch is never used.
- **Restricted tools**: the agent gets a fixed ``--allowedTools`` set and a
  ``--disallowedTools`` denylist (no push, no merge, no network fetch); it
  cannot widen its own permissions directly, though see the TODO below on
  the ``Bash(python:*)`` allowance.
- **Timeout**: the subprocess is killed at the limit and the issue fails.
- **Single flight**: a lock file permits one execution at a time.
- **Fail closed**: missing ``claude`` CLI, missing repo, dirty lock — all
  refuse or fail; there is no auto-retry.
- **Never merges**: preparing a fix branch is the ceiling; merging is human.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from overwatch.config import Config
from overwatch.redact import redact_text
from overwatch.store import Store, TransitionError

# Ceiling for one remediation attempt; chosen to comfortably cover a
# read-investigate-edit-test cycle without leaving a runaway agent attached.
DEFAULT_TIMEOUT_S = 600
# A lock older than this is assumed abandoned (crash, kill -9, power loss)
# rather than a live execution, so a new attempt may reclaim it. Set well
# above DEFAULT_TIMEOUT_S so a merely-slow-but-alive run is never preempted.
LOCK_STALE_S = 2 * DEFAULT_TIMEOUT_S

# The complete tool surface granted to the headless agent. Read/Edit/Write/
# Glob/Grep let it inspect and modify files in the worktree; the two Bash
# entries let it invoke Python (e.g. to run a project's test suite) but grant
# no other shell command.
#
# TODO(comprehension): ALLOWED_TOOLS grants Bash(python:*)/Bash(python3:*), and a
# Python process can itself `subprocess` out to `git push`, `curl`, etc.
# DISALLOWED_TOOLS below matches literal Bash command prefixes. IF the `claude`
# CLI's own tool gating inspects only Bash's argv this way, and does not also
# see what a permitted python/python3 child process later executes, THEN a
# permitted python invocation could reach the actions DISALLOWED_TOOLS exists to
# block. Confirm the CLI's actual gating behavior before treating this denylist
# as airtight — see the module docstring's "Restricted tools" rail, which this
# TODO qualifies rather than contradicts.
ALLOWED_TOOLS = "Read Edit Write Glob Grep Bash(python:*) Bash(python3:*)"
# Explicit denylist layered on top of the allowlist: blocks the specific
# Bash-invoked actions (push, merge, gh, network fetch, recursive delete)
# that would let a "prepare a fix branch" run escape into publishing,
# merging, or destroying data — the escape hatches this executor exists to
# prevent even under approved/auto-eligible execution.
DISALLOWED_TOOLS = (
    "WebFetch WebSearch Bash(git push:*) Bash(git merge:*) Bash(gh:*) "
    "Bash(curl:*) Bash(wget:*) Bash(rm:*)"
)

# Branch names the executor must never operate on. In practice every branch
# built by `execute()` is `fix/<issue_id>-<slug>`, which always carries a
# numeric issue-id prefix and so can never literally equal one of these — the
# check below is defense-in-depth kept in case the branch template changes,
# not a guard that is presently reachable.
_FORBIDDEN_BRANCHES = frozenset({"main", "master", "production"})


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one call to :meth:`Executor.execute`.

    Attributes:
        status: One of ``"dry-run"``, ``"completed"``, ``"failed"``, or
            ``"refused"``. ``"refused"`` means the issue/solution lookup,
            authorization, or the single-flight lock blocked the call before
            anything was touched. ``"failed"`` is broader than "an attempt was
            made": it also covers preconditions checked before any attempt —
            no usable target repo, ``claude`` missing from PATH, a forbidden
            branch name (all via :meth:`_fail`, called before the worktree or
            subprocess exist) — in addition to a spawned attempt that timed
            out, exited non-zero, or raised. ``"dry-run"`` is its own status,
            returned unconditionally by the dry-run branch; it can never
            surface as ``"failed"``.
        detail: Human-readable explanation, shown to the operator.
        command: The ``claude`` CLI invocation that was (or would be) run.
        transcript_path: Where the redacted transcript was written, if any.
        branch: The fix branch name, once one was computed.
    """

    status: str  # "dry-run" | "completed" | "failed" | "refused"
    detail: str
    command: list[str] | None = None
    transcript_path: Path | None = None
    branch: str | None = None


def _slug(text: str, max_len: int = 30) -> str:
    """Turn free-text into a short, branch-name-safe slug.

    Args:
        text: Arbitrary text (typically an issue title).
        max_len: Maximum slug length before trailing separators are trimmed.

    Returns:
        Lowercase, hyphen-separated slug; ``"issue"`` if nothing usable
        remains after stripping non-alphanumeric characters.
    """
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "issue"


def _git(repo: Path, *args: str) -> str:
    """Run a git subcommand against ``repo`` and return its trimmed stdout.

    Args:
        repo: Path to the git repository (``-C`` target).
        *args: The git subcommand and its arguments, e.g. ``"worktree", "add"``.

    Returns:
        Trimmed stdout on success.

    Raises:
        RuntimeError: If git exits non-zero; message carries the first two
            args plus a truncated stderr for a legible failure reason.
        subprocess.TimeoutExpired: If git does not finish within 60s — a
            generous bound against a hang (e.g. a stale ``index.lock``)
            rather than blocking the caller indefinitely.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _run_claude(
    command: list[str], cwd: Path, timeout_s: int
) -> subprocess.CompletedProcess[str]:
    """The single spawn point — isolated so tests can prove dry-run never spawns.

    Args:
        command: The full ``claude`` CLI invocation (binary + flags + prompt).
        cwd: Working directory the subprocess is launched in — always the
            dedicated fix-branch worktree, never the operator's checkout.
        timeout_s: Seconds before the child is killed.

    Returns:
        The completed subprocess, with stdout/stderr captured as text.

    Raises:
        subprocess.TimeoutExpired: If the child has not exited within
            ``timeout_s``. ``subprocess.run`` kills the child itself when
            this fires, so no process is left running past the timeout.
    """
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout_s
    )


class ExecutorLock:
    """One execution at a time per data dir, via a pid lock file.

    The lock file lives at ``cfg.data_dir / "executor.lock"`` — per-data-dir,
    not machine-wide — but a single file there (not one per issue) still
    enforces that only one remediation agent runs concurrently within that
    data dir, regardless of which issue it is fixing — avoids multiple agents
    racing worktrees/branches against the same or different target repos.
    """

    def __init__(self, data_dir: Path) -> None:
        """Args:
        data_dir: Overwatch's data directory; the lock file lives directly
            under it as ``executor.lock``.
        """
        self.path = data_dir / "executor.lock"

    def acquire(self) -> bool:
        """Try to take the lock.

        Returns:
            ``True`` if the lock was acquired (a lock file now exists and is
            owned by this call); ``False`` if another execution holds it, or
            if lock-file creation failed for any reason (fails closed rather
            than assuming the lock is free).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
                if age < LOCK_STALE_S:
                    return False
                self.path.unlink(missing_ok=True)  # reclaim a stale lock
            except OSError:
                return False
        # Atomic create: O_EXCL fails if another racer created the lock between
        # the existence check above and here, so only one caller can win.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False  # lost the create race (or cannot create) — fail closed
        try:
            os.write(fd, f"{os.getpid()} {datetime.now(UTC).isoformat()}".encode())
        finally:
            os.close(fd)
        return True

    def release(self) -> None:
        """Drop the lock. Safe to call even if it was never acquired."""
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


class Executor:
    """Runs one approved/auto-eligible solution as a sandboxed headless agent.

    This is the only place in the codebase that spawns the ``claude`` CLI to
    act on a solution. Every call to :meth:`execute` re-checks authorization
    (via :meth:`_authorize`, which reads the solution's stored
    ``auto_eligible`` flag and, for a gated solution, its recorded approval
    decision — it does not re-run the gate classifier itself) and
    recomputes the target repo and the branch/command from scratch; nothing
    here trusts state computed earlier by a caller.
    """

    def __init__(self, store: Store, cfg: Config) -> None:
        """Args:
        store: Shared persistence layer (issue/solution/approval records).
        cfg: Loaded configuration — supplies ``dry_run``, ``targets``, and
            the data directory the lock/worktrees/transcripts live under.
        """
        self.store = store
        self.cfg = cfg
        self.lock = ExecutorLock(cfg.data_dir)

    # -- preconditions -----------------------------------------------------

    def _authorize(self, issue: dict[str, Any], solution: dict[str, Any]) -> str | None:
        """Return a refusal reason, or None when execution is permitted.

        An auto-eligible solution (``kind`` on the gate classifier's safe
        allowlist, with no gate pattern matched — see
        :mod:`overwatch.detect.gate_classifier`) is authorized unconditionally
        here; it never needs an approval record. A gated solution requires
        exactly one recorded decision and that decision must be "approved" —
        "denied", or no decision at all, both refuse.

        Args:
            issue: Row dict from :meth:`Store.get_issue`.
            solution: Row dict from :meth:`Store.get_solution`.

        Returns:
            ``None`` if authorized, otherwise a short human-readable reason.
        """
        if solution["auto_eligible"]:
            return None
        approval = self.store.approval_for_solution(solution["id"])
        if approval is None:
            return "gated solution has no recorded approval"
        if approval["decision"] != "approved":
            return f"solution was {approval['decision']}"
        return None

    def _target_repo(self, issue: dict[str, Any]) -> Path | None:
        """Resolve the configured repo the issue's evidence says it came from.

        Args:
            issue: Row dict from :meth:`Store.get_issue`; its
                ``evidence["target"]`` is matched against configured target
                names (never an arbitrary path from the issue itself, so a
                tampered/foreign evidence blob cannot point the executor at
                an unconfigured filesystem location).

        Returns:
            The matching :class:`~overwatch.config.Target`'s repo path, or
            ``None`` if no configured target matches or that target has no
            repo (e.g. a log-only target).
        """
        target_name = (issue.get("evidence") or {}).get("target")
        for t in self.cfg.targets:
            if t.name == target_name and t.repo is not None:
                return t.repo
        return None

    # -- main entry ----------------------------------------------------------

    def execute(
        self, issue_id: int, solution_id: int, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> ExecutionResult:
        """Authorize, then (dry-run permitting) run a solution end to end.

        Args:
            issue_id: The issue the solution addresses.
            solution_id: The solution to execute; must belong to ``issue_id``.
            timeout_s: Seconds to allow the spawned ``claude`` CLI to run
                before it is killed and the attempt is recorded as failed.

        Returns:
            An :class:`ExecutionResult` describing what happened (or would
            have happened, for a dry run). This method never raises for an
            expected failure mode — git/subprocess/lifecycle errors are
            caught and turned into a ``"failed"`` result.
        """
        # --- Step 1: load and cross-check the issue/solution pair ---
        issue = self.store.get_issue(issue_id)
        solution = self.store.get_solution(solution_id)
        if issue is None or solution is None or solution["issue_id"] != issue_id:
            return ExecutionResult("refused", "issue/solution not found or mismatched")

        # --- Step 2: authorize (approval gate for gated solutions) ---
        refusal = self._authorize(issue, solution)
        if refusal:
            return ExecutionResult("refused", refusal)

        # --- Step 3: resolve the repo to work in and the runner binary ---
        repo = self._target_repo(issue)
        if repo is None or not (repo / ".git").exists():
            return self._fail(issue, "no usable target repo for this issue")

        claude = shutil.which("claude")
        if claude is None:
            # Fail closed rather than silently no-op: an operator watching
            # for "completed"/"failed" should never see nothing happen.
            return self._fail(issue, "claude CLI not found on PATH (failing closed)")

        # --- Step 4: compute the isolated fix branch (never the default) ---
        branch = f"fix/{issue_id}-{_slug(issue['title'])}"
        if branch.split("/")[-1] in _FORBIDDEN_BRANCHES or branch in _FORBIDDEN_BRANCHES:
            return self._fail(issue, f"refusing forbidden branch name {branch!r}")

        # --- Step 5: build the sandboxed CLI invocation ---
        prompt = self._build_prompt(issue, solution, branch)
        command = [
            claude,
            "-p",
            prompt,
            "--allowedTools",
            ALLOWED_TOOLS,
            "--disallowedTools",
            DISALLOWED_TOOLS,
            "--permission-mode",
            "acceptEdits",  # auto-accept file edits inside the worktree only;
            # the tool lists above are what actually bound the blast radius.
        ]

        transcript = self._transcript_path(issue_id)
        # --- Step 6: dry-run short-circuit — the safe default (Config.dry_run
        # is True unless the operator opts in). Nothing below this point runs:
        # no lock, no worktree, no subprocess. ---
        if self.cfg.dry_run:
            self._write_transcript(
                transcript,
                f"DRY-RUN (nothing spawned)\nbranch: {branch}\nrepo: {repo}\n"
                f"command: {command}\n\nprompt:\n{prompt}",
            )
            return ExecutionResult(
                "dry-run",
                "dry_run is on: planned command recorded, nothing executed",
                command=command,
                transcript_path=transcript,
                branch=branch,
            )

        # --- Step 7: single-flight lock, then prepare the worktree and run ---
        if not self.lock.acquire():
            return ExecutionResult("refused", "another execution is in flight")

        worktree = self.cfg.data_dir / "worktrees" / f"issue-{issue_id}"
        try:
            self.store.set_issue_status(issue_id, "executing")
            _git(repo, "worktree", "add", "-b", branch, str(worktree))
            try:
                proc = _run_claude(command, cwd=worktree, timeout_s=timeout_s)
            except subprocess.TimeoutExpired:
                # subprocess.run kills the child on timeout, so no orphaned
                # agent process survives this branch.
                self._write_transcript(transcript, f"TIMEOUT after {timeout_s}s\nbranch: {branch}")
                self.store.set_issue_status(issue_id, "failed")
                return ExecutionResult(
                    "failed", f"timed out after {timeout_s}s", command, transcript, branch
                )
            self._write_transcript(
                transcript,
                f"branch: {branch}\nexit: {proc.returncode}\n\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
            )
            # --- Step 8: interpret the outcome — a zero exit only means the
            # agent finished; it may have written FINDINGS.md instead of a
            # fix (see the prompt below), so "completed" is not a promise the
            # issue is actually resolved, only that a branch is ready to
            # review. ---
            if proc.returncode == 0:
                self.store.set_issue_status(issue_id, "resolved")
                return ExecutionResult(
                    "completed",
                    f"fix prepared on branch {branch!r}; review and merge is a human step",
                    command,
                    transcript,
                    branch,
                )
            self.store.set_issue_status(issue_id, "failed")
            return ExecutionResult(
                "failed", f"agent exited {proc.returncode}", command, transcript, branch
            )
        except (RuntimeError, OSError, TransitionError) as exc:
            # Covers a failed `git worktree add`, filesystem errors writing
            # the transcript, and an illegal status transition — all folded
            # into the same "failed" result rather than propagating, since a
            # caller (the HTTP API) should never see a raw exception here.
            with contextlib.suppress(Exception):
                self.store.set_issue_status(issue_id, "failed")
            return ExecutionResult("failed", str(exc)[:300], command, transcript, branch)
        finally:
            self.lock.release()
            # The worktree (and its branch) stay behind for human review.

    # -- helpers ------------------------------------------------------------

    def _fail(self, issue: dict[str, Any], reason: str) -> ExecutionResult:
        """Record a pre-execution failure and return the ``"failed"`` result.

        Only transitions the issue to "failed" when it is already
        "executing" — the store's transition table (see
        :mod:`overwatch.store`) has no direct "drafted"/"pending_approval" ->
        "failed" edge, so a precondition failure that happens before
        ``set_issue_status(..., "executing")`` runs must leave the status
        alone rather than raise :class:`TransitionError` on top of the
        original failure.

        Args:
            issue: Row dict from :meth:`Store.get_issue`.
            reason: Human-readable refusal/failure reason.

        Returns:
            ``ExecutionResult("failed", reason)``.
        """
        try:
            if issue["status"] in ("drafted", "pending_approval"):
                # not started; leave lifecycle where it is
                pass
            elif issue["status"] == "executing":
                self.store.set_issue_status(issue["id"], "failed")
        except Exception:
            # Best-effort bookkeeping: never let a status-transition error
            # here mask the original failure `reason` returned below.
            pass
        return ExecutionResult("failed", reason)

    def _transcript_path(self, issue_id: int) -> Path:
        """Build a fresh, timestamped transcript path for one execution attempt.

        A new filename per attempt (rather than one file per issue) keeps a
        full history across retries instead of overwriting the previous run.
        """
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.cfg.transcripts_dir / f"issue-{issue_id}-{ts}.log"

    def _write_transcript(self, path: Path, content: str) -> None:
        """Redact and persist a transcript.

        Every transcript — dry-run plan, timeout notice, or full stdout/
        stderr — passes through :func:`overwatch.redact.redact_text` first,
        since the executed agent's output can legitimately contain
        secret-shaped strings read from the target repo.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact_text(content), encoding="utf-8")

    def _build_prompt(
        self, issue: dict[str, Any], solution: dict[str, Any], branch: str
    ) -> str:
        """Compose the headless agent's instructions.

        This is a soft (prompt-level) constraint layer on top of the hard
        tool restrictions in ``ALLOWED_TOOLS``/``DISALLOWED_TOOLS`` — it
        tells the agent not to switch branches, push, merge, or touch
        main/master, and to stop and write ``FINDINGS.md`` instead of
        forcing an unsafe or unclear fix. Nothing here is technically
        enforced; it relies on the agent following instructions.

        Args:
            issue: Row dict from :meth:`Store.get_issue`.
            solution: Row dict from :meth:`Store.get_solution`; its
                ``body_md`` (the operator-facing solution brief) is included
                verbatim.
            branch: The fix branch the agent is told it is already on.

        Returns:
            The full prompt string passed to ``claude -p``.
        """
        return (
            "You are an automated remediation agent operating under strict rails.\n"
            f"You are in a dedicated git worktree on branch {branch!r}.\n"
            "Rules (non-negotiable):\n"
            "- Work ONLY inside the current directory.\n"
            "- Commit your changes to the CURRENT branch. Never switch branch, "
            "never push, never merge, never touch main/master.\n"
            "- Run the project's tests if present and report results honestly.\n"
            "- If the fix is unsafe or unclear, stop and write FINDINGS.md instead.\n\n"
            f"Issue (seen {issue.get('count', 1)}x, severity {issue.get('severity')}):\n"
            f"{issue.get('title')}\n\n"
            f"Solution brief:\n{solution.get('body_md')}\n"
        )
