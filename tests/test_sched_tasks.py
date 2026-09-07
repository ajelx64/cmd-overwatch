"""Scheduled-task analyzer tests. Record shape mirrors the live PowerShell
query output (ISO datetimes with offset); all values synthetic.

Covers ``overwatch.collector.sched_tasks``: ``analyze()`` (the pure,
platform-independent function that turns Task Scheduler-shaped records into
Findings -- last-run failure, disabled, stuck-running, missed/overdue runs),
``_powershell_exe()`` (pwsh-vs-Windows-PowerShell selection), ``query_tasks()``
(subprocess invocation and its JSON-shape/error-code handling, mocked here so
these tests run on non-Windows CI), and ``scan()`` (the query+analyze glue).
The ``record()`` helper builds one healthy baseline task dict that individual
tests override via keyword args, so each test's diff from "healthy" states
exactly which field it is exercising.
"""

import json as _json
import subprocess as _subprocess
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

import overwatch.collector.sched_tasks as sched_mod
from overwatch.collector.sched_tasks import _powershell_exe, analyze, query_tasks, scan

# Fixed instant every test's task records are analyzed against, so relative
# ages (e.g. "last run 5 hours ago") are deterministic instead of drifting
# with wall-clock time.
NOW = datetime.fromisoformat("2026-01-06T12:00:00-07:00")


def record(**overrides: Any) -> dict[str, Any]:
    """Build one healthy Task Scheduler record, overridden per test.

    The baseline is a "Ready" task that ran on schedule with result 0 and no
    missed runs -- i.e. it should produce zero findings from analyze(). Each
    test passes only the field(s) it wants to make unhealthy.
    """
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
    """The baseline record() with no overrides must produce zero findings."""
    assert analyze([record()], NOW) == []


def test_failed_last_result() -> None:
    """A nonzero LastTaskResult must produce exactly one high-severity
    finding, with the hex-formatted code echoed in both title and evidence."""
    findings = analyze([record(LastTaskResult=1)], NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "high"
    assert "0x1" in f.title
    assert f.evidence["last_task_result_hex"] == "0x1"


def test_benign_result_codes_skipped() -> None:
    """The three known non-failure result codes (currently running, never
    yet run, terminated by user request) must not be flagged as failures."""
    for code in (0x41301, 0x41303, 0x41306):  # running / never ran / terminated by user
        assert analyze([record(LastTaskResult=code)], NOW) == []


def test_disabled_task() -> None:
    """State == "Disabled" must produce exactly one medium-severity finding."""
    findings = analyze([record(State="Disabled")], NOW)
    assert [f.severity for f in findings] == ["medium"]
    assert "disabled" in findings[0].title


def test_running_recent_is_fine() -> None:
    """A task still Running after only 1 hour (well under the 12h stuck
    threshold) must not be flagged."""
    rec = record(State="Running", LastRunTime="2026-01-06T11:00:00-07:00", LastTaskResult=0x41301)
    assert analyze([rec], NOW) == []


def test_stuck_running_flagged() -> None:
    """A task still Running 25 hours after its LastRunTime (past the 12h
    STUCK_RUNNING_HOURS threshold) must be flagged."""
    rec = record(State="Running", LastRunTime="2026-01-05T11:00:00-07:00", LastTaskResult=0x41301)
    findings = analyze([rec], NOW)
    assert len(findings) == 1
    assert "stuck" in findings[0].title


def test_missed_runs_flagged() -> None:
    """A nonzero MissedRuns count must be flagged, with the count named in
    the finding title."""
    findings = analyze([record(MissedRuns=3)], NOW)
    assert len(findings) == 1
    assert "3 missed" in findings[0].title


def test_next_run_in_past_flagged() -> None:
    """A "Ready" task whose NextRunTime has already passed (beyond the 1h
    MISSED_GRACE_HOURS grace period) must be flagged even with MissedRuns=0
    -- catches a schedule that silently stopped advancing."""
    findings = analyze([record(NextRunTime="2026-01-06T09:00:00-07:00")], NOW)
    assert len(findings) == 1
    assert "past" in findings[0].title


def test_failure_and_disabled_stack() -> None:
    """A task that is both disabled and has a failing last result must
    produce both findings (high + medium), not just one -- the checks are
    independent, not mutually exclusive."""
    findings = analyze([record(State="Disabled", LastTaskResult=0x420)], NOW)
    assert {f.severity for f in findings} == {"high", "medium"}


def test_unparseable_datetimes_tolerated() -> None:
    """A malformed LastRunTime (the legacy "/Date(...)/ " shape some Windows
    APIs still emit) and a missing NextRunTime must not raise -- _parse_dt
    returns None, and the stuck/overdue checks that need a parsed datetime
    simply don't fire rather than crashing the whole scan."""
    rec = record(LastRunTime="/Date(1736175600000)/", NextRunTime=None)
    assert analyze([rec], NOW) == []


def test_fingerprint_stable_across_changing_codes() -> None:
    """Two different failing result codes for the same task must share one
    fingerprint -- the fingerprint keys on "this task's last run failed",
    not on which specific error code, so a flapping code doesn't spawn a
    new issue every run."""
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
    # spy exists only to prove subprocess.run is never reached for the
    # empty-folders short-circuit; its failure message is the assertion.
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
        """Stand in for subprocess.run to simulate a successful PowerShell query
        returning a JSON list, so query_tasks's parsing path runs on any OS.
        """
        return _subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(data), stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    result = query_tasks(("\\Jobs\\",))
    assert result == data


def test_query_tasks_single_object_wrapped_in_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """When JSON returns a single object (not a list) it is wrapped in a list.

    PowerShell's ConvertTo-Json collapses a one-element array to a bare
    object, so query_tasks must normalize that shape back to a list itself.
    """
    single = {"TaskPath": "\\Jobs\\", "TaskName": "Sync", "State": "Ready",
              "LastRunTime": None, "LastTaskResult": 0,
              "NextRunTime": None, "MissedRuns": 0}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        """Stand in for subprocess.run to simulate PowerShell collapsing a single-task
        result to a bare JSON object instead of a list.
        """
        return _subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(single), stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    result = query_tasks(("\\Jobs\\",))
    assert result == [single]


def test_query_tasks_empty_stdout_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout from subprocess yields an empty list (no tasks found)."""
    def fake_run(cmd: list[str], **kw: Any) -> Any:
        """Stand in for subprocess.run to simulate PowerShell finding no matching tasks."""
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sched_mod.subprocess, "run", fake_run)
    assert query_tasks(("\\Missing\\",)) == []


def test_query_tasks_nonzero_returncode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero returncode from Get-ScheduledTask must raise RuntimeError."""
    def fake_run(cmd: list[str], **kw: Any) -> Any:
        """Stand in for subprocess.run to simulate Get-ScheduledTask failing
        (e.g. access denied to the requested folder).
        """
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
