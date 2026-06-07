"""Log purge: delete day-log files older than the configured retention period."""
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

    Returns (files_deleted, bytes_freed).
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

    label = "dry-run" if dry_run else "live"
    print(f"[purge] {target.name}: {files_deleted} file(s), {bytes_freed} bytes ({label})")

    store.add_log_purge_run(target.name, files_deleted, bytes_freed, dry_run=dry_run)

    return files_deleted, bytes_freed


def purge_all(cfg: Config, store: Store, now: datetime | None = None) -> None:
    """Purge logs for all configured targets and record in the store."""
    for target in cfg.targets:
        purge_target(target, cfg.retention_days, store, cfg.dry_run, now=now)
