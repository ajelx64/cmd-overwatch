"""Log purge: delete day-log files older than the configured retention period.

Invoked once per collector pass by the collector entrypoint
(``overwatch.collector.__main__``), after findings from the current pass have
already been persisted — so a log file is never removed before this run's own
scan of it has been recorded. Depends on ``overwatch.config`` for the
per-target log location/glob and retention setting, and on ``overwatch.store``
to record each purge run for the dashboard's audit trail.

This module deletes files. A file is deleted only if it: matches the target's
``log_glob`` inside the target's ``log_dir``; is a regular file per
``Path.is_file()`` (directories are skipped); still resolves to a path inside
``log_dir`` after resolving symlinks and ``..`` segments (guards against a
glob match that is secretly a symlink pointing outside the log tree); and has
a modification time older than ``retention_days`` before ``now``. Everything
else is spared: files at or after the cutoff, files that resolve outside
``log_dir``, targets with no ``log_dir`` configured, and targets whose
``log_dir`` does not exist. When ``dry_run`` is true (the collector's
default; see ``overwatch.config``), nothing is deleted — candidates are only
printed and counted — though a purge-run record is still written to the store
so a dry-run pass is visible in the dashboard's history.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from overwatch.config import Config, Target
from overwatch.store import Store


def purge_target(
    target: Target,
    retention_days: int,
    store: Store,
    dry_run: bool,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Purge log files older than retention_days for one target.

    Args:
        target: Target configuration naming the log directory and glob. A
            target with no ``log_dir`` configured, or whose ``log_dir``
            doesn't exist, is a no-op.
        retention_days: Files whose modification time is older than this many
            days (from ``now``) are deleted; files at or after the cutoff are
            kept.
        store: Store to record this purge run in, regardless of dry-run.
        dry_run: If true, only print what would be deleted — no file is
            actually removed — but the counts and store record still reflect
            what *would* have happened.
        now: Reference time for the cutoff calculation; defaults to the
            current UTC time. Overridable so tests don't depend on the wall
            clock.

    Returns:
        ``(files_deleted, bytes_freed)`` — in dry-run mode these count the
        files that *would* be deleted, not files actually removed.
    """
    if target.log_dir is None:
        return 0, 0

    log_dir: Path = target.log_dir
    if not log_dir.exists():
        return 0, 0

    if now is None:
        now = datetime.now(UTC)

    cutoff = now - timedelta(days=retention_days)

    files_deleted = 0
    bytes_freed = 0

    # --- Step 1: walk files matching this target's glob, deleting eligible ones ---
    for path in log_dir.glob(target.log_glob):
        if not path.is_file():
            continue
        # Resolve to ensure the file is inside log_dir (no traversal outside)
        try:
            path.resolve().relative_to(log_dir.resolve())
        except ValueError:
            continue

        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime >= cutoff:
            continue

        file_size = path.stat().st_size
        if dry_run:
            print(f"[purge] {target.name}: would delete {path.name} ({file_size} bytes)")
        else:
            path.unlink()

        files_deleted += 1
        bytes_freed += file_size

    # --- Step 2: report and record this run, live or dry-run either way ---
    label = "dry-run" if dry_run else "live"
    print(f"[purge] {target.name}: {files_deleted} file(s), {bytes_freed} bytes ({label})")

    store.add_log_purge_run(target.name, files_deleted, bytes_freed, dry_run=dry_run)

    return files_deleted, bytes_freed


def purge_all(cfg: Config, store: Store, now: datetime | None = None) -> None:
    """Purge logs for all configured targets and record in the store.

    Single flat loop over targets, delegating to :func:`purge_target` for
    each — no further phases to narrate here.

    Args:
        cfg: Collector configuration providing the target list, retention
            window, and dry-run flag applied uniformly to every target.
        store: Store to record each target's purge run in.
        now: Reference time forwarded to :func:`purge_target`; defaults to
            the current UTC time.
    """
    for target in cfg.targets:
        purge_target(target, cfg.retention_days, store, cfg.dry_run, now=now)
