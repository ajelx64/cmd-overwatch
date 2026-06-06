"""Scheduled-task analyzer tests. Record shape mirrors the live PowerShell
query output (ISO datetimes with offset); all values synthetic."""

from datetime import datetime
from typing import Any

from overwatch.collector.sched_tasks import analyze

NOW = datetime.fromisoformat("2026-01-06T12:00:00-07:00")


def record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "TaskPath": "\\Jobs\\",
        "TaskName": "Nightly",
        "State": "Ready",
        "LastRunTime": "2026-01-06T07:00:00-07:00",
        "LastTaskResult": 0,
        "NextRunTime": "2026-01-07T07:00:00-07:00",
        "MissedRuns": 0,
    }
    base.update(overrides)
    return base


def test_healthy_task_yields_nothing() -> None:
    assert analyze([record()], NOW) == []


def test_failed_last_result() -> None:
    findings = analyze([record(LastTaskResult=1)], NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "high"
    assert "0x1" in f.title
    assert f.evidence["last_task_result_hex"] == "0x1"


def test_benign_result_codes_skipped() -> None:
    for code in (0x41301, 0x41303, 0x41306):  # running / never ran / terminated by user
        assert analyze([record(LastTaskResult=code)], NOW) == []


def test_disabled_task() -> None:
    findings = analyze([record(State="Disabled")], NOW)
    assert [f.severity for f in findings] == ["medium"]
    assert "disabled" in findings[0].title


def test_running_recent_is_fine() -> None:
    rec = record(State="Running", LastRunTime="2026-01-06T11:00:00-07:00", LastTaskResult=0x41301)
    assert analyze([rec], NOW) == []


def test_stuck_running_flagged() -> None:
    rec = record(State="Running", LastRunTime="2026-01-05T11:00:00-07:00", LastTaskResult=0x41301)
    findings = analyze([rec], NOW)
    assert len(findings) == 1
    assert "stuck" in findings[0].title


def test_missed_runs_flagged() -> None:
    findings = analyze([record(MissedRuns=3)], NOW)
    assert len(findings) == 1
    assert "3 missed" in findings[0].title


def test_next_run_in_past_flagged() -> None:
    findings = analyze([record(NextRunTime="2026-01-06T09:00:00-07:00")], NOW)
    assert len(findings) == 1
    assert "past" in findings[0].title


def test_failure_and_disabled_stack() -> None:
    findings = analyze([record(State="Disabled", LastTaskResult=0x420)], NOW)
    assert {f.severity for f in findings} == {"high", "medium"}


def test_unparseable_datetimes_tolerated() -> None:
    rec = record(LastRunTime="/Date(1736175600000)/", NextRunTime=None)
    assert analyze([rec], NOW) == []


def test_fingerprint_stable_across_changing_codes() -> None:
    a = analyze([record(LastTaskResult=1)], NOW)[0]
    b = analyze([record(LastTaskResult=0x420)], NOW)[0]
    assert a.fingerprint == b.fingerprint  # same task, same problem class
