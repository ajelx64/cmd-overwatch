"""Signal (c): git hygiene per target repo.

Subprocess collection (:func:`collect_repo_state`) is separated from the pure
:func:`analyze` so detection logic is fixture-testable without real repos.
All git commands run as ``git -C <repo>`` — never ``cd``, never the parent
directory.

Detected:

- uncommitted changes idle for > 24h (newest dirty-file mtime)  -> medium
- commits on local branches that exist on no remote             -> medium
- non-default branches with no commits for > 30 days            -> low
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from overwatch.detect.rules import Finding, make_fingerprint

SOURCE = "git_hygiene"

DIRTY_IDLE_HOURS = 24
STALE_BRANCH_DAYS = 30
_GIT_TIMEOUT = 30


@dataclass
class RepoState:
    """Snapshot of the hygiene-relevant repo facts."""

    dirty_files: list[str] = field(default_factory=list)
    newest_dirty_mtime: float | None = None  # epoch seconds
    unpushed_commits: list[str] = field(default_factory=list)  # oneline entries
    # (branch, last_commit_epoch) for local branches except the default one
    branches: list[tuple[str, float]] = field(default_factory=list)
    default_branch: str = "main"
    error: str | None = None  # set when the repo could not be read


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    return proc.stdout


def collect_repo_state(repo: Path) -> RepoState:
    """Read-only snapshot; failures land in ``state.error``."""
    state = RepoState()
    try:
        porcelain = _git(repo, "status", "--porcelain")
        state.dirty_files = [ln[3:].strip() for ln in porcelain.splitlines() if ln.strip()]
        if state.dirty_files:
            mtimes = []
            for rel in state.dirty_files:
                p = repo / rel
                if p.exists():
                    mtimes.append(p.stat().st_mtime)
            state.newest_dirty_mtime = max(mtimes) if mtimes else None

        unpushed = _git(repo, "log", "--branches", "--not", "--remotes", "--oneline")
        state.unpushed_commits = [ln for ln in unpushed.splitlines() if ln.strip()]

        head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "main"
        state.default_branch = head.strip() or "main"
        refs = _git(
            repo, "for-each-ref", "refs/heads", "--format=%(refname:short)\x1f%(committerdate:unix)"
        )
        for ln in refs.splitlines():
            if "\x1f" not in ln:
                continue
            branch, _, epoch = ln.partition("\x1f")
            if branch != state.default_branch and epoch.strip().isdigit():
                state.branches.append((branch, float(epoch)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        state.error = str(exc)
    return state


def analyze(name: str, state: RepoState, now: float | None = None) -> list[Finding]:
    """Pure analysis of a repo snapshot."""
    now = now if now is not None else time.time()
    findings: list[Finding] = []

    if state.error is not None:
        findings.append(
            Finding(
                fingerprint=make_fingerprint(name, "repo-unreadable"),
                source=SOURCE,
                severity="medium",
                title=f"{name}: repo could not be read ({state.error[:60]})",
                evidence={"target": name, "error": state.error},
            )
        )
        return findings

    if (
        state.dirty_files
        and state.newest_dirty_mtime is not None
        and now - state.newest_dirty_mtime > DIRTY_IDLE_HOURS * 3600
    ):
        idle_h = int((now - state.newest_dirty_mtime) / 3600)
        findings.append(
            Finding(
                fingerprint=make_fingerprint(name, "dirty-idle"),
                source=SOURCE,
                severity="medium",
                title=f"{name}: {len(state.dirty_files)} uncommitted change(s) idle for {idle_h}h",
                evidence={
                    "target": name,
                    "dirty_files": state.dirty_files[:20],
                    "idle_hours": idle_h,
                },
            )
        )

    if state.unpushed_commits:
        findings.append(
            Finding(
                fingerprint=make_fingerprint(name, "unpushed"),
                source=SOURCE,
                severity="medium",
                title=f"{name}: {len(state.unpushed_commits)} commit(s) not on any remote",
                evidence={"target": name, "commits": state.unpushed_commits[:10]},
            )
        )

    stale = [
        (b, e) for b, e in state.branches if now - e > STALE_BRANCH_DAYS * 86400
    ]
    if stale:
        findings.append(
            Finding(
                fingerprint=make_fingerprint(name, "stale-branches"),
                source=SOURCE,
                severity="low",
                title=f"{name}: {len(stale)} stale branch(es) older than {STALE_BRANCH_DAYS}d",
                evidence={"target": name, "branches": [b for b, _ in stale][:10]},
            )
        )
    return findings


def scan_repo(name: str, repo: Path) -> list[Finding]:
    """Collect + analyze one repo."""
    if not (repo / ".git").exists():
        return analyze(name, RepoState(error="not a git repository"))
    return analyze(name, collect_repo_state(repo))
