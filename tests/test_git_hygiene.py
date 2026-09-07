"""Tests for ``overwatch.collector.git_hygiene``: the git-hygiene detection signal.

Two layers: pure ``analyze()`` tests drive synthetic ``RepoState`` snapshots
(no real git involved, so severity/threshold edge cases are deterministic),
and integration tests run real git commands against throwaway repos created
fresh under ``tmp_path`` to confirm ``collect_repo_state``/``scan_repo`` read
real git state correctly. Every repo used in this file is created by the test
itself — nothing here reads or reports on any repository other than these
disposable fixtures.
"""

import subprocess
import time
from pathlib import Path

from overwatch.collector.git_hygiene import RepoState, analyze, collect_repo_state, scan_repo

NOW = 1_900_000_000.0  # fixed epoch for pure tests
HOUR = 3600.0
DAY = 86400.0


# -- pure analysis -----------------------------------------------------------


def test_clean_repo_yields_nothing() -> None:
    """A snapshot with nothing dirty, unpushed, or stale yields no findings —
    the baseline that guards against analyze() ever false-positiving on a
    genuinely clean repo.
    """
    assert analyze("proj", RepoState(), NOW) == []


def test_fresh_dirty_changes_not_flagged() -> None:
    """Dirty files younger than the idle threshold are not flagged.

    Ordinary work in progress from the last couple of hours must not
    generate noise; only changes left uncommitted past DIRTY_IDLE_HOURS do.
    """
    state = RepoState(dirty_files=["a.py"], newest_dirty_mtime=NOW - 2 * HOUR)
    assert analyze("proj", state, NOW) == []


def test_idle_dirty_changes_flagged() -> None:
    """Dirty files idle past the 24h threshold are flagged, with the
    reported idle-hour count matching actual elapsed time (30h), not just a
    boolean crossing.
    """
    state = RepoState(dirty_files=["a.py", "b.py"], newest_dirty_mtime=NOW - 30 * HOUR)
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert "2 uncommitted" in findings[0].title
    assert findings[0].evidence["idle_hours"] == 30


def test_unpushed_commits_flagged() -> None:
    """Any commit that exists on no remote is flagged, with no idle grace
    period — unlike dirty files, unpushed commits are flagged immediately.
    """
    state = RepoState(unpushed_commits=["abc fix thing", "def add thing"])
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert "2 commit(s) not on any remote" in findings[0].title


def test_stale_branch_flagged_fresh_ignored() -> None:
    """Branches idle past 30 days are flagged low-severity; a recently
    touched branch in the same repo is not, and only the stale one appears
    in the evidence list — confirms per-branch filtering, not an
    all-or-nothing verdict on the whole repo.
    """
    state = RepoState(
        branches=[("old-feature", NOW - 45 * DAY), ("fresh-feature", NOW - 2 * DAY)]
    )
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].evidence["branches"] == ["old-feature"]


def test_unreadable_repo_reported_once() -> None:
    """A snapshot carrying an error short-circuits to exactly one finding.

    analyze() must not also run the dirty/unpushed/stale checks against a
    RepoState it couldn't populate — those would all read empty defaults and
    either fabricate a second finding or mask the real problem.
    """
    findings = analyze("proj", RepoState(error="boom"), NOW)
    assert len(findings) == 1
    assert "could not be read" in findings[0].title


# -- integration on real temporary repos --------------------------------------


def _run(repo: Path, *args: str) -> None:
    """Run a git command against a throwaway test repo, failing loudly on error."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    """Build a minimal one-commit git repo with no remote under tmp_path.

    Shared setup for the integration tests below — none of them care about
    file content, only about the git state (dirty files, branches, commits)
    layered on top afterward.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "test@example.invalid")
    _run(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "init")
    return repo


def test_collect_state_clean_repo(tmp_path: Path) -> None:
    """collect_repo_state reads a freshly made repo's real git state: no
    dirty files, HEAD reflected as default_branch, and (no remote
    configured) the init commit counted as unpushed.
    """
    repo = make_repo(tmp_path)
    state = collect_repo_state(repo)
    assert state.error is None
    assert state.dirty_files == []
    assert state.default_branch == "main"
    # no remotes configured -> the init commit is unpushed
    assert len(state.unpushed_commits) == 1


def test_collect_state_dirty_file(tmp_path: Path) -> None:
    """An untracked file is picked up as dirty with a real, current mtime.

    The 60s tolerance guards against timing slop between the test writing
    the file and stat() reading it back, not against a collector bug.
    """
    repo = make_repo(tmp_path)
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    state = collect_repo_state(repo)
    assert "new.py" in state.dirty_files
    assert state.newest_dirty_mtime is not None
    assert abs(state.newest_dirty_mtime - time.time()) < 60


def test_collect_state_side_branch(tmp_path: Path) -> None:
    """A non-default branch is reported in state.branches.

    Implicitly confirms the default branch itself is excluded from this
    list (per the module docstring) — only non-default branches are
    stale-branch candidates.
    """
    repo = make_repo(tmp_path)
    _run(repo, "branch", "feature/x")
    state = collect_repo_state(repo)
    assert [b for b, _ in state.branches] == ["feature/x"]


def test_scan_repo_non_git_dir(tmp_path: Path) -> None:
    """Pointing scan_repo at a plain directory (no .git) reports one
    finding instead of raising — a misconfigured target must not crash the
    collector pass.
    """
    findings = scan_repo("proj", tmp_path)
    assert len(findings) == 1
    assert "could not be read" in findings[0].title
