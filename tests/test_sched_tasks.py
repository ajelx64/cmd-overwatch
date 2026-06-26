"""Scheduled-task analyzer tests. Record shape mirrors the live PowerShell
query output (ISO datetimes with offset); all values synthetic."""

import json as _json
import subprocess as _subprocess
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

import overwatch.collector.sched_tasks as sched_mod
from overwatch.collector.sched_tasks import _powershell_exe, analyze, query_tasks, scan

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


# ---------------------------------------------------------------------------
# _powershell_exe coverage
# ---------------------------------------------------------------------------


def test_powershell_exe_prefers_pwsh_when_available() -> None:
    """_powershell_exe returns the pwsh path when pwsh is on PATH."""
    with patch("shutil.which", return_value="C:/Program Files/PowerShell/pwsh.exe"):
        result = _powershell_exe()
    assert result == "C:/Program Files/PowerShell/pwsh.exe"


def test_powershell_exe_falls_back_to_powershell_when_pwsh_absent() -> None:
    """_powershell_exe falls back to 'powershell' when pwsh is not found."""
    with patch("shutil.which", return_value=None):
        result = _powershell_exe()
    assert result == "powershell"


# ---------------------------------------------------------------------------
# query_tasks coverage
# ---------------------------------------------------------------------------


def test_query_tasks_empty_folders_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_tasks with no folders must return [] without spawning a subprocess."""
    def spy(*a: Any, **k: Any) -> None:
        raise AssertionError("subprocess.run must not be called for empty task folders")

    monkeypatch.setattr(sched_mod.subprocess, "run", spy)
    assert query_tasks(()) == []


def test_query_tasks_success_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_tasks parses a JSON list from a successful subprocess run."""
    data = [{"TaskPath": "\\Jobs\\", "TaskName": "Sync", "State": "Ready",
             "LastRunTime": None, "LastTaskResult": 0,
             "NextRunTime": None, "MissedRuns": 0}]

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        return _subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(data), stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    result = query_tasks(("\\Jobs\\",))
    assert result == data


def test_query_tasks_single_object_wrapped_in_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """When JSON returns a single object (not a list) it is wrapped in a list."""
    single = {"TaskPath": "\\Jobs\\", "TaskName": "Sync", "State": "Ready",
              "LastRunTime": None, "LastTaskResult": 0,
              "NextRunTime": None, "MissedRuns": 0}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        return _subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(single), stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    result = query_tasks(("\\Jobs\\",))
    assert result == [single]


def test_query_tasks_empty_stdout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout from subprocess yields an empty list (no tasks found)."""
    def fake_run(cmd: list[str], **kw: Any) -> Any:
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    assert query_tasks(("\\Missing\\",)) == []


def test_query_tasks_nonzero_returncode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero returncode from Get-ScheduledTask must raise RuntimeError."""
    def fake_run(cmd: list[str], **kw: Any) -> Any:
        return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Access denied")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Get-ScheduledTask query failed"):
        query_tasks(("\\Restricted\\",))


# ---------------------------------------------------------------------------
# scan() integration coverage
# ---------------------------------------------------------------------------


def test_scan_integrates_query_and_analyze(monkeypatch: pytest.MonkeyPatch) -> None:
    """scan() delegates to query_tasks then analyze; the composition is tested here."""
    tasks = [record(LastTaskResult=1)]  # one failure -> one high finding

    monkeypatch.setattr(sched_mod, "query_tasks", lambda folders: tasks)
    findings = scan(("\\Jobs\\",), now=NOW)
    assert len(findings) == 1
    assert findings[0].severity == "high"
