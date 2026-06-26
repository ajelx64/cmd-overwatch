"""Host health tests: disk thresholds, event summaries, log freshness."""

import os
import subprocess as _subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import overwatch.collector.host_health as hh_mod
from overwatch.collector.host_health import (
    analyze_windows_events,
    check_disk,
    check_log_freshness,
    check_windows_events,
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


# ---------------------------------------------------------------------------
# check_disk: OSError and threshold branches (coverage gaps)
# ---------------------------------------------------------------------------

_FakeUsage = SimpleNamespace


def _make_usage(total: int, free: int) -> SimpleNamespace:
    return _FakeUsage(total=total, used=total - free, free=free)


def test_check_disk_oserror_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError from disk_usage (e.g. unmounted path) must silently return empty."""
    def raise_oserror(_: object) -> None:
        raise OSError("no disk")

    monkeypatch.setattr(hh_mod.shutil, "disk_usage", raise_oserror)
    findings, metrics = check_disk(Path("/nonexistent"))
    assert findings == []
    assert metrics == []


def test_check_disk_critical_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free < DISK_CRITICAL_PCT must produce a critical severity finding."""
    # 5% free is below DISK_CRITICAL_PCT (10%)
    total = 1_000_000_000
    free = int(total * 0.05)
    monkeypatch.setattr(hh_mod.shutil, "disk_usage", lambda _: _make_usage(total, free))
    findings, metrics = check_disk(Path("."))
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert not metrics[0].healthy


def test_check_disk_warning_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """DISK_CRITICAL_PCT <= free < DISK_WARN_PCT must produce a medium severity finding."""
    # 12% free: above critical (10%), below warning (15%)
    total = 1_000_000_000
    free = int(total * 0.12)
    monkeypatch.setattr(hh_mod.shutil, "disk_usage", lambda _: _make_usage(total, free))
    findings, metrics = check_disk(Path("."))
    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert not metrics[0].healthy


# ---------------------------------------------------------------------------
# check_windows_events: subprocess paths and non-Windows early return
# ---------------------------------------------------------------------------


def test_check_windows_events_non_windows_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a non-Windows platform the function returns immediately with nothing."""
    monkeypatch.setattr(hh_mod.sys, "platform", "linux")
    findings, metrics = check_windows_events()
    assert findings == []
    assert metrics == []


def test_check_windows_events_oserror_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError while spawning PowerShell must degrade gracefully."""
    def raise_oserror(*a: object, **k: object) -> None:
        raise OSError("no powershell")

    monkeypatch.setattr(hh_mod.sys, "platform", "win32")
    # Python 3.13's shutil.which consults _winapi when sys.platform == "win32";
    # _winapi is None on a non-Windows CI runner, so stub which() to keep the
    # faked-win32 path from crashing before the subprocess mock is exercised.
    monkeypatch.setattr(hh_mod.shutil, "which", lambda *_a, **_k: "pwsh")
    monkeypatch.setattr(hh_mod.subprocess, "run", raise_oserror)
    findings, metrics = check_windows_events()
    assert findings == []
    assert metrics == []


def test_check_windows_events_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutExpired from PowerShell must degrade gracefully."""
    def raise_timeout(*a: object, **k: object) -> None:
        raise _subprocess.TimeoutExpired(cmd="pwsh", timeout=90)

    monkeypatch.setattr(hh_mod.sys, "platform", "win32")
    # Python 3.13's shutil.which consults _winapi when sys.platform == "win32";
    # _winapi is None on a non-Windows CI runner, so stub which() to keep the
    # faked-win32 path from crashing before the subprocess mock is exercised.
    monkeypatch.setattr(hh_mod.shutil, "which", lambda *_a, **_k: "pwsh")
    monkeypatch.setattr(hh_mod.subprocess, "run", raise_timeout)
    findings, metrics = check_windows_events()
    assert findings == []
    assert metrics == []


def test_check_windows_events_nonzero_exit_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero returncode from PowerShell must degrade gracefully."""
    def fake_run(cmd: list[str], **kw: object) -> object:
        return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Access denied")

    monkeypatch.setattr(hh_mod.sys, "platform", "win32")
    # Python 3.13's shutil.which consults _winapi when sys.platform == "win32";
    # _winapi is None on a non-Windows CI runner, so stub which() to keep the
    # faked-win32 path from crashing before the subprocess mock is exercised.
    monkeypatch.setattr(hh_mod.shutil, "which", lambda *_a, **_k: "pwsh")
    monkeypatch.setattr(hh_mod.subprocess, "run", fake_run)
    findings, metrics = check_windows_events()
    assert findings == []
    assert metrics == []


def test_check_windows_events_bad_json_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON from PowerShell must degrade gracefully."""
    def fake_run(cmd: list[str], **kw: object) -> object:
        return _subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

    monkeypatch.setattr(hh_mod.sys, "platform", "win32")
    # Python 3.13's shutil.which consults _winapi when sys.platform == "win32";
    # _winapi is None on a non-Windows CI runner, so stub which() to keep the
    # faked-win32 path from crashing before the subprocess mock is exercised.
    monkeypatch.setattr(hh_mod.shutil, "which", lambda *_a, **_k: "pwsh")
    monkeypatch.setattr(hh_mod.subprocess, "run", fake_run)
    findings, metrics = check_windows_events()
    assert findings == []
    assert metrics == []


# ---------------------------------------------------------------------------
# check_log_freshness: OSError from stat() (coverage gap)
# ---------------------------------------------------------------------------


def test_check_log_freshness_stat_oserror_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError from Path.stat() on a globbed log file must be silently skipped.

    When ALL stat() calls fail the function should flag 'no logs found'
    (the same as if the directory were empty), not raise.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "job.log").write_text("=== exit 0 ===\n", encoding="utf-8")

    # Patch Path.stat so that .log files raise OSError
    _original_stat = Path.stat

    def bad_stat(self: Path, *, follow_symlinks: bool = True) -> object:
        if self.suffix == ".log":
            raise OSError("permission denied")
        return _original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", bad_stat)
    target = Target(name="t", log_dir=log_dir, max_log_age_hours=24)
    findings = check_log_freshness(target, NOW)
    # All stat calls failed → treated as "no logs found"
    assert len(findings) == 1
    assert "no logs found" in findings[0].title
