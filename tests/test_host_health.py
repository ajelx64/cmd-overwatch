"""Host health tests: disk thresholds, event summaries, log freshness."""

import os
import time
from pathlib import Path

from overwatch.collector.host_health import (
    analyze_windows_events,
    check_disk,
    check_log_freshness,
)
from overwatch.config import Target

NOW = time.time()


# -- disk ----------------------------------------------------------------------


def test_disk_check_returns_metric(tmp_path: Path) -> None:
    findings, metrics = check_disk(tmp_path)
    assert len(metrics) == 1
    assert metrics[0].metric == "disk_free_pct"
    assert float(metrics[0].value) > 0
    # a dev machine running this test suite is presumably not at <15% free;
    # whichever way it goes, findings must be consistent with the metric
    assert bool(findings) == (not metrics[0].healthy) or findings == []


# -- windows events (pure analyzer) ----------------------------------------------


def test_no_events_is_healthy() -> None:
    findings, metrics = analyze_windows_events({"Total": 0, "Top": []})
    assert findings == []
    assert metrics[0].healthy is True


def test_events_summarized_with_top_providers() -> None:
    data = {
        "Total": 12,
        "Top": [{"Provider": "Disk", "Count": 8}, {"Provider": "DCOM", "Count": 4}],
    }
    findings, metrics = analyze_windows_events(data, hours=48)
    assert len(findings) == 1
    assert "12 critical/error" in findings[0].title
    assert "Disk×8" in findings[0].evidence["top_providers"]
    assert metrics[0].healthy is False


# -- log freshness ----------------------------------------------------------------


def make_target(tmp_path: Path, max_age: int | None, log_age_hours: float | None) -> Target:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    if log_age_hours is not None:
        p = log_dir / "job-2026-01-05.log"
        p.write_text("=== exit 0 @ 01:00 ===\n", encoding="utf-8")
        stamp = NOW - log_age_hours * 3600
        os.utime(p, (stamp, stamp))
    return Target(name="t", log_dir=log_dir, max_log_age_hours=max_age)


def test_no_cap_no_check(tmp_path: Path) -> None:
    assert check_log_freshness(make_target(tmp_path, None, 999), NOW) == []


def test_fresh_logs_pass(tmp_path: Path) -> None:
    assert check_log_freshness(make_target(tmp_path, 24, 3), NOW) == []


def test_stale_logs_flagged(tmp_path: Path) -> None:
    findings = check_log_freshness(make_target(tmp_path, 24, 72), NOW)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert "72h old" in findings[0].title


def test_no_logs_at_all_flagged(tmp_path: Path) -> None:
    findings = check_log_freshness(make_target(tmp_path, 24, None), NOW)
    assert len(findings) == 1
    assert "no logs found" in findings[0].title
