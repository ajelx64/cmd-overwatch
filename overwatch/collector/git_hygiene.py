"""Signal (c): git hygiene per target repo.

Subprocess collection (:func:`collect_repo_state`) is separated from the pure
:func:`analyze` so detection logic is fixture-testable without real repos.
All git commands run as ``git -C <repo>`` — never ``cd``, never the parent
directory.

Detected:

- uncommitted changes idle for > 24h (newest dirty-file mtime)  -> medium
- commits on local branches that exist on no remote             -> medium
- non-default branches with no commits for > 30 days            -> low

Invoked by the collector entrypoint (``overwatch.collector.__main__``) for each
configured target that has a ``repo`` path. Depends on ``overwatch.detect.rules``
for the shared ``Finding``/fingerprint vocabulary and on ``git`` being on PATH.
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
    """Run one ``git -C <repo> <args>`` command and return its stdout.

    Args:
        repo: Repository working directory. Passed via ``-C`` (never ``cd``)
            so this process's own working directory is never touched.
        *args: Git subcommand and its arguments, e.g. ``"status",
            "--porcelain"``.

    Returns:
        The command's stdout, unparsed.

    Raises:
        RuntimeError: If git exits non-zero; the message is its stderr,
            truncated to 200 chars so a pathological error can't bloat a
            finding's evidence.
    """
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
    """Read-only snapshot; failures land in ``state.error``.

    Args:
        repo: Path to the repository's working directory.

    Returns:
        A :class:`RepoState`. On any git failure the partial state collected
        so far is discarded in favor of ``state.error`` — callers only see a
        clean snapshot or a clear "couldn't read this repo" signal, never a
        half-filled one.
    """
    state = RepoState()
    try:
        # --- Step 1: uncommitted changes and how long they've sat there ---
        porcelain = _git(repo, "status", "--porcelain")
        # `git status --porcelain` lines are "XY <path>" (two status chars +
        # a space); slicing off the first 3 chars recovers the path.
        state.dirty_files = [ln[3:].strip() for ln in porcelain.splitlines() if ln.strip()]
        if state.dirty_files:
            mtimes = []
            for rel in state.dirty_files:
                p = repo / rel
                if p.exists():
                    mtimes.append(p.stat().st_mtime)
            state.newest_dirty_mtime = max(mtimes) if mtimes else None

        # --- Step 2: commits that exist locally but on no remote-tracking branch ---
        unpushed = _git(repo, "log", "--branches", "--not", "--remotes", "--oneline")
        state.unpushed_commits = [ln for ln in unpushed.splitlines() if ln.strip()]

        # --- Step 3: local branches (excluding the default) and their last commit time ---
        head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "main"
        state.default_branch = head.strip() or "main"
        refs = _git(
            repo, "for-each-ref", "refs/heads", "--format=%(refname:short)\x1f%(committerdate:unix)"
        )
        for ln in refs.splitlines():
            if "\x1f" not in ln:
                continue
            # \x1f (unit separator) is used as the field delimiter because it
            # cannot appear in a branch name, unlike a space or comma.
            branch, _, epoch = ln.partition("\x1f")
            if branch != state.default_branch and epoch.strip().isdigit():
                state.branches.append((branch, float(epoch)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        # Any collection step failing (git missing, timeout, unreadable repo)
        # aborts the whole snapshot rather than reporting partial facts.
        state.error = str(exc)
    return state


def analyze(name: str, state: RepoState, now: float | None = None) -> list[Finding]:
    """Pure analysis of a repo snapshot.

    Args:
        name: Target name to attribute findings to (not the repo path — this
            keeps fingerprints and titles stable even if the repo moves).
        state: A snapshot from :func:`collect_repo_state` (or a hand-built
            fixture in tests).
        now: Reference epoch time for the age-based checks; defaults to the
            current time. Overridable so tests don't depend on the wall
            clock.

    Returns:
        Findings for whichever of the three hygiene checks the snapshot
        trips; a repo can trigger more than one. If ``state.error`` is set,
        every other check is skipped — there is nothing further to analyze.
    """
    now = now if now is not None else time.time()
    findings: list[Finding] = []

    # --- Step 1: the repo could not be read at all -> stop here ---
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

    # --- Step 2: uncommitted changes that have sat untouched too long ---
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

    # --- Step 3: commits that exist only locally, on no remote ---
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

    # --- Step 4: non-default branches that have gone cold ---
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
    """Collect + analyze one repo.

    Single flat guard-then-delegate: check that ``repo`` looks like a git
    repository, then hand off to the two already-documented steps.

    Args:
        name: Target name to attribute findings to.
        repo: Path to the repository's working directory.

    Returns:
        Findings from :func:`analyze`. A missing ``.git`` directory is
        reported the same way as any other unreadable-repo error, so callers
        don't need a separate case for "not actually a repo".
    """
    if not (repo / ".git").exists():
        return analyze(name, RepoState(error="not a git repository"))
    return analyze(name, collect_repo_state(repo))
