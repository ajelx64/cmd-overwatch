"""Log purge tests: retention enforcement, dry-run, glob filtering, audit rows.

Covers ``overwatch.collector.log_purge``: ``purge_target`` (age-based file
deletion for one :class:`~overwatch.config.Target`, live or dry-run, glob
filtering, missing-dir handling) and ``purge_all`` (fan-out across every
configured target). Every test builds its own ``tmp_path`` log directory and
a throwaway :class:`~overwatch.store.Store` (SQLite file under the same
``tmp_path``), then backdates file mtimes with ``os.utime`` to simulate age
without waiting real time. Be exact per test about which files are asserted
*deleted* (age > retention, glob match, inside ``log_dir``) versus *spared*
(recent mtime, non-matching glob, dry-run) -- purging is destructive, so a
docstring here must not claim more than the assertions check.
"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from overwatch.collector.log_purge import purge_all, purge_target
from overwatch.config import Config, Target
from overwatch.store import Store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> Store:
    """Create a throwaway Store backed by a fresh SQLite file under tmp_path."""
    return Store(tmp_path / "test.db")


def _old_ts(days: int = 40) -> float:
    """Return a Unix timestamp `days` days in the past."""
    return time.time() - days * 86400


def _new_ts(days: int = 10) -> float:
    """Return a Unix timestamp `days` days in the past (recent)."""
    return time.time() - days * 86400


def _set_mtime(path: Path, ts: float) -> None:
    """Backdate a file's mtime so age-based purge logic can be tested
    without sleeping in real time."""
    os.utime(path, (ts, ts))


def _make_log_dir(tmp_path: Path, name: str = "logs") -> Path:
    """Create and return an empty log directory under tmp_path."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fixed_now() -> datetime:
    """A single reference 'now' shared by a test's setup and its purge_target
    call, so the 40-day/10-day fixture ages below are computed against the
    same instant the purge cutoff is computed against."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_purge_deletes_old_files(tmp_path: Path) -> None:
    """A file older than the 30-day retention window is deleted; a file
    younger than it is spared -- proves the age cutoff, not just "delete
    everything in the glob"."""
    # --- Arrange ---
    log_dir = _make_log_dir(tmp_path)
    old1 = log_dir / "job-old-1.log"
    old2 = log_dir / "job-old-2.log"
    recent = log_dir / "job-recent.log"
    for f in (old1, old2, recent):
        f.write_text("log content", encoding="utf-8")

    now = _fixed_now()
    old_ts = _old_ts(40)
    _set_mtime(old1, old_ts)
    _set_mtime(old2, old_ts)
    _set_mtime(recent, _new_ts(10))

    store = _make_store(tmp_path)
    target = Target(name="my-project", log_dir=log_dir)
    # --- Act ---
    files_deleted, bytes_freed = purge_target(target, 30, store, dry_run=False, now=now)

    # --- Assert ---
    assert files_deleted == 2
    assert bytes_freed > 0
    # The two 40-day-old files are gone from disk...
    assert not old1.exists()
    assert not old2.exists()
    # ...but the 10-day-old file, inside the same 30-day window, survives.
    assert recent.exists()

    row = store._conn.execute("SELECT * FROM log_purge_runs WHERE target = 'my-project'").fetchone()
    assert row is not None
    assert row["files_deleted"] == 2
    assert row["dry_run"] == 0
    store.close()


def test_purge_dry_run_deletes_nothing(tmp_path: Path) -> None:
    """dry_run=True must delete nothing on disk while still reporting the
    counts it *would* have deleted and still writing the audit row (with
    dry_run=1) -- proves dry-run is observability-only, not a no-op."""
    # --- Arrange ---
    log_dir = _make_log_dir(tmp_path)
    old1 = log_dir / "job-old-1.log"
    old2 = log_dir / "job-old-2.log"
    recent = log_dir / "job-recent.log"
    for f in (old1, old2, recent):
        f.write_text("log content", encoding="utf-8")

    now = _fixed_now()
    old_ts = _old_ts(40)
    _set_mtime(old1, old_ts)
    _set_mtime(old2, old_ts)
    _set_mtime(recent, _new_ts(10))

    store = _make_store(tmp_path)
    target = Target(name="my-project", log_dir=log_dir)
    # --- Act ---
    files_deleted, bytes_freed = purge_target(target, 30, store, dry_run=True, now=now)

    # --- Assert ---
    # Files still exist — dry run must not delete
    assert old1.exists()
    assert old2.exists()
    assert recent.exists()

    # Counts still reflect what would have been deleted
    assert files_deleted == 2

    # Audit row IS written with dry_run=True
    row = store._conn.execute("SELECT * FROM log_purge_runs WHERE target = 'my-project'").fetchone()
    assert row is not None
    assert row["files_deleted"] == 2
    assert row["dry_run"] == 1
    store.close()


def test_purge_skips_non_matching_files(tmp_path: Path) -> None:
    """With log_glob="*.log", an old .log file is deleted but an equally old
    .txt file in the same directory is spared -- proves the glob filter
    gates deletion, age alone is not enough."""
    # --- Arrange ---
    log_dir = _make_log_dir(tmp_path)
    old_log = log_dir / "job.log"
    old_txt = log_dir / "notes.txt"
    for f in (old_log, old_txt):
        f.write_text("content", encoding="utf-8")

    now = _fixed_now()
    old_ts = _old_ts(40)
    _set_mtime(old_log, old_ts)
    _set_mtime(old_txt, old_ts)

    store = _make_store(tmp_path)
    target = Target(name="my-project", log_dir=log_dir, log_glob="*.log")
    # --- Act ---
    files_deleted, _ = purge_target(target, 30, store, dry_run=False, now=now)

    # --- Assert ---
    assert files_deleted == 1
    assert not old_log.exists()
    assert old_txt.exists()  # .txt must survive
    store.close()


def test_purge_skips_missing_log_dir(tmp_path: Path) -> None:
    """A target whose log_dir does not exist on disk yields zero deletions
    and writes no audit row at all -- distinguishes "nothing to purge"
    (this case) from "purged and found nothing" (which still logs a row)."""
    missing_dir = tmp_path / "nonexistent"
    store = _make_store(tmp_path)
    target = Target(name="ghost", log_dir=missing_dir)
    files_deleted, bytes_freed = purge_target(target, 30, store, dry_run=False)

    assert files_deleted == 0
    assert bytes_freed == 0
    row = store._conn.execute("SELECT COUNT(*) AS n FROM log_purge_runs").fetchone()
    assert row["n"] == 0
    store.close()


def test_purge_all_records_per_target(tmp_path: Path) -> None:
    """purge_all fans out across every configured target and writes one
    audit row per target, in target order -- not a single combined row."""
    # --- Arrange ---
    log_dir1 = _make_log_dir(tmp_path, "logs1")
    log_dir2 = _make_log_dir(tmp_path, "logs2")

    now = _fixed_now()
    old_ts = _old_ts(40)

    for d in (log_dir1, log_dir2):
        f = d / "job.log"
        f.write_text("data", encoding="utf-8")
        _set_mtime(f, old_ts)

    store = _make_store(tmp_path)
    cfg = Config(
        targets=(
            Target(name="target-a", log_dir=log_dir1),
            Target(name="target-b", log_dir=log_dir2),
        ),
        dry_run=False,
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )
    # --- Act ---
    purge_all(cfg, store, now=now)

    # --- Assert ---
    rows = store._conn.execute("SELECT target FROM log_purge_runs ORDER BY id").fetchall()
    assert [r["target"] for r in rows] == ["target-a", "target-b"]
    store.close()


def test_purge_empty_dir_no_eligible(tmp_path: Path) -> None:
    """A log_dir containing only a recent file deletes nothing but still
    records an audit row with files_deleted=0 -- a purge run that found
    nothing to do is still a recorded run."""
    log_dir = _make_log_dir(tmp_path)
    recent = log_dir / "job-recent.log"
    recent.write_text("content", encoding="utf-8")
    _set_mtime(recent, _new_ts(10))

    now = _fixed_now()
    store = _make_store(tmp_path)
    target = Target(name="fresh", log_dir=log_dir)
    files_deleted, bytes_freed = purge_target(target, 30, store, dry_run=False, now=now)

    assert files_deleted == 0
    assert bytes_freed == 0
    row = store._conn.execute("SELECT * FROM log_purge_runs WHERE target = 'fresh'").fetchone()
    assert row is not None
    assert row["files_deleted"] == 0
    store.close()


def test_purge_target_none_log_dir(tmp_path: Path) -> None:
    """A target with log_dir=None (repo-only target) is a no-op purge that
    writes no audit row -- there is no log directory to have purged."""
    store = _make_store(tmp_path)
    target = Target(name="no-logs", log_dir=None, repo=tmp_path)
    files_deleted, bytes_freed = purge_target(target, 30, store, dry_run=False)

    assert files_deleted == 0
    assert bytes_freed == 0
    row = store._conn.execute("SELECT COUNT(*) AS n FROM log_purge_runs").fetchone()
    assert row["n"] == 0
    store.close()
