"""Tests for ``overwatch.aar.generator``: report rendering, persistence, and
the re-run-append behaviour.

Uses a real (temp-file) Store rather than mocks, seeded via ``seed()`` below
with one issue in each report-relevant status (pending approval, resolved,
failed) plus a host-health metric and a log-purge run — one seed exercises
every section ``render()`` produces.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from overwatch.aar.generator import generate, render
from overwatch.config import Config
from overwatch.store import Store

DAY = date(2026, 1, 6)
NOW = datetime(2026, 1, 6, 7, 30)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Store, Config]:
    """A dry-run Config paired with its own on-disk Store, isolated per test."""
    cfg = Config(data_dir=tmp_path / "data", reports_dir=tmp_path / "reports", dry_run=True)
    store = Store(cfg.db_path)
    return store, cfg


def seed(store: Store) -> dict[str, int]:
    """Populate one issue per report-relevant status, plus a host-health
    metric and a log-purge run, so a single call exercises every section
    render() produces.

    Returns the created issue ids, though the current tests below discard
    them (kept for any future test that needs to target a specific issue).
    """
    pending = store.upsert_issue(
        "fp-p", "log_scan", "high", "proj/job: run failed with exit 1", {"target": "proj"}
    )
    store.add_solution(pending, "plan", "uncertain", auto_eligible=False)
    store.set_issue_status(pending, "drafted")
    store.set_issue_status(pending, "pending_approval")

    resolved = store.upsert_issue("fp-r", "sched_tasks", "medium", "task missed", {"target": "x"})
    store.set_issue_status(resolved, "drafted")
    store.set_issue_status(resolved, "executing")
    store.set_issue_status(resolved, "resolved")

    failed = store.upsert_issue("fp-f", "log_scan", "high", "boom", {"target": "proj"})
    store.set_issue_status(failed, "drafted")
    store.set_issue_status(failed, "executing")
    store.set_issue_status(failed, "failed")

    store.add_host_health("disk_free_pct", "42.0", True)
    store.add_log_purge_run("proj", 3, 4096, dry_run=True)
    return {"pending": pending, "resolved": resolved, "failed": failed}


def test_render_contains_all_sections(env: tuple[Store, Config]) -> None:
    """render() emits every section heading and reflects the seeded state:
    dry-run mode, the pending issue's gate category, and the purge run's
    dry-run marker all show up in the rendered text.

    A missing heading here means an operator's daily brief silently dropped
    a whole section — this is the regression guard for that.
    """
    store, cfg = env
    seed(store)
    # render()'s "opened today" filter matches each issue's real first_seen
    # timestamp (set by the store at insert time), not an arbitrary
    # report_date — so this must be today's actual date, not the fixed DAY.
    today = datetime.now(UTC).date()
    body, summary = render(store, cfg, today, NOW)
    for heading in (
        "# After-Action Report",
        "## Health board",
        "### Host metrics",
        "## Issues opened today",
        "## Issues resolved today",
        "## Awaiting approval",
        "## Failed executions",
        "## Agent transcripts today",
        "## Log purge activity",
    ):
        assert heading in body, heading
    assert "DRY-RUN" in body
    assert "gate: **uncertain**" in body
    assert "3 file(s), 4096 bytes (dry-run)" in body
    assert "1 pending approval" in summary


def test_generate_writes_file_and_record(env: tuple[Store, Config]) -> None:
    """generate() writes the report file at the expected path and records
    it in aar_records, so the dashboard can look up "today's" report by date.
    """
    store, cfg = env
    seed(store)
    path = generate(store, cfg, DAY, NOW)
    assert path.name == "2026-01-06-aar.md"
    assert path.exists()
    record = store.latest_aar()
    assert record is not None
    assert record["report_date"] == "2026-01-06"
    assert record["path"] == str(path)


def test_rerun_appends_not_overwrites(env: tuple[Store, Config]) -> None:
    """A second generate() call for the same day appends a timestamped
    Re-run section instead of overwriting the first report.

    This is the behaviour the module docstring promises ("the day's story
    accumulates") — losing it would silently erase the morning's findings
    whenever the collector re-runs later the same day.
    """
    store, cfg = env
    seed(store)
    path = generate(store, cfg, DAY, NOW)
    first = path.read_text(encoding="utf-8")
    generate(store, cfg, DAY, datetime(2026, 1, 6, 12, 0))
    second = path.read_text(encoding="utf-8")
    assert second.startswith(first)
    assert "## Re-run 12:00" in second
    assert second.count("# After-Action Report") == 2


def test_empty_store_renders_green(env: tuple[Store, Config]) -> None:
    """With no issues at all, render() reports an explicit all-clear rather
    than an empty or malformed health board.
    """
    store, cfg = env
    body, summary = render(store, cfg, DAY, NOW)
    assert "All targets green" in body
    assert "0 active" in summary
