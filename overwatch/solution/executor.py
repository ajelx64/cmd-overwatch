"""Headless executor: run an approved solution via ``claude -p`` under rails.

Safety rails (all enforced here, none optional):

- **Approval**: a gated solution executes only with a recorded ``approved``
  decision; auto solutions only when their stored draft is auto-eligible.
- **Dry-run default**: with ``dry_run`` on (the default), the exact planned
  command is recorded to the transcript and *nothing is spawned*.
- **Worktree isolation**: work happens on a fresh ``fix/<issue>-<slug>``
  branch in a dedicated ``git worktree`` under overwatch's data dir — the
  operator's checkout is untouched and the default branch is never used.
- **Restricted tools**: the agent gets a fixed ``--allowedTools`` set and a
  ``--disallowedTools`` denylist (no push, no merge, no network fetch); it
  cannot widen its own permissions.
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

DEFAULT_TIMEOUT_S = 600
LOCK_STALE_S = 2 * DEFAULT_TIMEOUT_S

ALLOWED_TOOLS = "Read Edit Write Glob Grep Bash(python:*) Bash(python3:*)"
DISALLOWED_TOOLS = (
    "WebFetch WebSearch Bash(git push:*) Bash(git merge:*) Bash(gh:*) "
    "Bash(curl:*) Bash(wget:*) Bash(rm:*)"
)

_FORBIDDEN_BRANCHES = frozenset({"main", "master", "production"})


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # "dry-run" | "completed" | "failed" | "refused"
    detail: str
    command: list[str] | None = None
    transcript_path: Path | None = None
    branch: str | None = None


def _slug(text: str, max_len: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "issue"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _run_claude(
    command: list[str], cwd: Path, timeout_s: int
) -> subprocess.CompletedProcess[str]:
    """The single spawn point — isolated so tests can prove dry-run never spawns."""
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout_s
    )


class ExecutorLock:
    """One execution at a time, machine-wide, via a pid lock file."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "executor.lock"

    def acquire(self) -> bool:
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
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


class Executor:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg
        self.lock = ExecutorLock(cfg.data_dir)

    # -- preconditions -----------------------------------------------------

    def _authorize(self, issue: dict[str, Any], solution: dict[str, Any]) -> str | None:
        """Return a refusal reason, or None when execution is permitted."""
        if solution["auto_eligible"]:
            return None
        approval = self.store.approval_for_solution(solution["id"])
        if approval is None:
            return "gated solution has no recorded approval"
        if approval["decision"] != "approved":
            return f"solution was {approval['decision']}"
        return None

    def _target_repo(self, issue: dict[str, Any]) -> Path | None:
        target_name = (issue.get("evidence") or {}).get("target")
        for t in self.cfg.targets:
            if t.name == target_name and t.repo is not None:
                return t.repo
        return None

    # -- main entry ----------------------------------------------------------

    def execute(
        self, issue_id: int, solution_id: int, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> ExecutionResult:
        issue = self.store.get_issue(issue_id)
        solution = self.store.get_solution(solution_id)
        if issue is None or solution is None or solution["issue_id"] != issue_id:
            return ExecutionResult("refused", "issue/solution not found or mismatched")

        refusal = self._authorize(issue, solution)
        if refusal:
            return ExecutionResult("refused", refusal)

        repo = self._target_repo(issue)
        if repo is None or not (repo / ".git").exists():
            return self._fail(issue, "no usable target repo for this issue")

        claude = shutil.which("claude")
        if claude is None:
            return self._fail(issue, "claude CLI not found on PATH (failing closed)")

        branch = f"fix/{issue_id}-{_slug(issue['title'])}"
        if branch.split("/")[-1] in _FORBIDDEN_BRANCHES or branch in _FORBIDDEN_BRANCHES:
            return self._fail(issue, f"refusing forbidden branch name {branch!r}")

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
            "acceptEdits",
        ]

        transcript = self._transcript_path(issue_id)
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

        if not self.lock.acquire():
            return ExecutionResult("refused", "another execution is in flight")

        worktree = self.cfg.data_dir / "worktrees" / f"issue-{issue_id}"
        try:
            self.store.set_issue_status(issue_id, "executing")
            _git(repo, "worktree", "add", "-b", branch, str(worktree))
            try:
                proc = _run_claude(command, cwd=worktree, timeout_s=timeout_s)
            except subprocess.TimeoutExpired:
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
            with contextlib.suppress(Exception):
                self.store.set_issue_status(issue_id, "failed")
            return ExecutionResult("failed", str(exc)[:300], command, transcript, branch)
        finally:
            self.lock.release()
            # The worktree (and its branch) stay behind for human review.

    # -- helpers ------------------------------------------------------------

    def _fail(self, issue: dict[str, Any], reason: str) -> ExecutionResult:
        try:
            if issue["status"] in ("drafted", "pending_approval"):
                # not started; leave lifecycle where it is
                pass
            elif issue["status"] == "executing":
                self.store.set_issue_status(issue["id"], "failed")
        except Exception:
            pass
        return ExecutionResult("failed", reason)

    def _transcript_path(self, issue_id: int) -> Path:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.cfg.transcripts_dir / f"issue-{issue_id}-{ts}.log"

    def _write_transcript(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact_text(content), encoding="utf-8")

    def _build_prompt(
        self, issue: dict[str, Any], solution: dict[str, Any], branch: str
    ) -> str:
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
