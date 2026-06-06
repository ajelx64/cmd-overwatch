"""Git hygiene tests: pure analyze() on snapshots + integration on real tmp repos."""

import subprocess
import time
from pathlib import Path

from overwatch.collector.git_hygiene import RepoState, analyze, collect_repo_state, scan_repo

NOW = 1_900_000_000.0  # fixed epoch for pure tests
HOUR = 3600.0
DAY = 86400.0


# -- pure analysis -----------------------------------------------------------


def test_clean_repo_yields_nothing() -> None:
    assert analyze("proj", RepoState(), NOW) == []


def test_fresh_dirty_changes_not_flagged() -> None:
    state = RepoState(dirty_files=["a.py"], newest_dirty_mtime=NOW - 2 * HOUR)
    assert analyze("proj", state, NOW) == []


def test_idle_dirty_changes_flagged() -> None:
    state = RepoState(dirty_files=["a.py", "b.py"], newest_dirty_mtime=NOW - 30 * HOUR)
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert "2 uncommitted" in findings[0].title
    assert findings[0].evidence["idle_hours"] == 30


def test_unpushed_commits_flagged() -> None:
    state = RepoState(unpushed_commits=["abc fix thing", "def add thing"])
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert "2 commit(s) not on any remote" in findings[0].title


def test_stale_branch_flagged_fresh_ignored() -> None:
    state = RepoState(
        branches=[("old-feature", NOW - 45 * DAY), ("fresh-feature", NOW - 2 * DAY)]
    )
    findings = analyze("proj", state, NOW)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].evidence["branches"] == ["old-feature"]


def test_unreadable_repo_reported_once() -> None:
    findings = analyze("proj", RepoState(error="boom"), NOW)
    assert len(findings) == 1
    assert "could not be read" in findings[0].title


# -- integration on real temporary repos --------------------------------------


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
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
    repo = make_repo(tmp_path)
    state = collect_repo_state(repo)
    assert state.error is None
    assert state.dirty_files == []
    assert state.default_branch == "main"
    # no remotes configured -> the init commit is unpushed
    assert len(state.unpushed_commits) == 1


def test_collect_state_dirty_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    state = collect_repo_state(repo)
    assert "new.py" in state.dirty_files
    assert state.newest_dirty_mtime is not None
    assert abs(state.newest_dirty_mtime - time.time()) < 60


def test_collect_state_side_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    _run(repo, "branch", "feature/x")
    state = collect_repo_state(repo)
    assert [b for b, _ in state.branches] == ["feature/x"]


def test_scan_repo_non_git_dir(tmp_path: Path) -> None:
    findings = scan_repo("proj", tmp_path)
    assert len(findings) == 1
    assert "could not be read" in findings[0].title
