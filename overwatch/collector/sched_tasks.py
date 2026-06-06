"""Signal (a): Windows Task Scheduler health for configured task folders.

The PowerShell query is isolated in :func:`query_tasks`; :func:`analyze` is a
pure function over the resulting records so it stays fixture-testable on any
platform (CI runs on Linux).

Detected:

- ``LastTaskResult`` is a real failure code -> high
- task ``Disabled``                         -> medium
- ``Running`` far longer than expected      -> medium (stuck)
- missed runs / next-run drifted into past  -> medium
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from typing import Any

from overwatch.detect.rules import Finding, make_fingerprint

SOURCE = "sched_tasks"

# Scheduler result codes that are not failures.
_OK_RESULTS = frozenset(
    {
        0x0,  # success
        0x41301,  # task is currently running
        0x41303,  # task has not yet run
        0x41306,  # last run terminated by user request (operator action, not a fault)
    }
)

STUCK_RUNNING_HOURS = 12
MISSED_GRACE_HOURS = 1

# Dates are forced to ISO-8601 ("o") in the query so the output parses the
# same under pwsh 7 and Windows PowerShell 5.1 (whose ConvertTo-Json would
# otherwise emit "/Date(...)/" and lacks -AsArray).
_PS_QUERY = (
    "$r = @(Get-ScheduledTask -TaskPath {paths} -ErrorAction SilentlyContinue "
    "| ForEach-Object {{ "
    "$i = $_ | Get-ScheduledTaskInfo; [PSCustomObject]@{{ "
    "TaskPath=$_.TaskPath; TaskName=$_.TaskName; State=[string]$_.State; "
    "LastRunTime=$(if ($i.LastRunTime) {{ $i.LastRunTime.ToString('o') }}); "
    "LastTaskResult=$i.LastTaskResult; "
    "NextRunTime=$(if ($i.NextRunTime) {{ $i.NextRunTime.ToString('o') }}); "
    "MissedRuns=$i.NumberOfMissedRuns }} }}); "
    "ConvertTo-Json -InputObject $r -Depth 3"
)


def _powershell_exe() -> str:
    """Prefer pwsh 7; fall back to Windows PowerShell 5.1."""
    import shutil

    return shutil.which("pwsh") or "powershell"


def query_tasks(task_folders: tuple[str, ...]) -> list[dict[str, Any]]:
    """Query Task Scheduler via PowerShell. Windows-only; raises on failure."""
    if not task_folders:
        return []
    paths = ",".join(f"'{f}'" for f in task_folders)
    cmd = _PS_QUERY.format(paths=paths)
    proc = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Get-ScheduledTask query failed: {proc.stderr.strip()[:200]}")
    out = proc.stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def analyze(tasks: list[dict[str, Any]], now: datetime) -> list[Finding]:
    """Pure analysis of task records (shape mirrors the PowerShell query)."""
    findings: list[Finding] = []
    for t in tasks:
        path = str(t.get("TaskPath", ""))
        name = str(t.get("TaskName", ""))
        full = f"{path}{name}"
        state = str(t.get("State", ""))
        result = t.get("LastTaskResult")
        last_run = _parse_dt(t.get("LastRunTime"))
        next_run = _parse_dt(t.get("NextRunTime"))
        missed = int(t.get("MissedRuns") or 0)

        if isinstance(result, int) and result not in _OK_RESULTS:
            findings.append(
                Finding(
                    fingerprint=make_fingerprint(full, "last-run-failed"),
                    source=SOURCE,
                    severity="high",
                    title=f"{full}: last run failed (0x{result:X})",
                    evidence={
                        "task": full,
                        "last_task_result": result,
                        "last_task_result_hex": f"0x{result:X}",
                        "last_run_time": str(t.get("LastRunTime")),
                        "state": state,
                    },
                )
            )

        if state == "Disabled":
            findings.append(
                Finding(
                    fingerprint=make_fingerprint(full, "disabled"),
                    source=SOURCE,
                    severity="medium",
                    title=f"{full}: task is disabled",
                    evidence={"task": full, "state": state},
                )
            )

        if (
            state == "Running"
            and last_run is not None
            and now - last_run > timedelta(hours=STUCK_RUNNING_HOURS)
        ):
            findings.append(
                Finding(
                    fingerprint=make_fingerprint(full, "stuck-running"),
                    source=SOURCE,
                    severity="medium",
                    title=f"{full}: running for over {STUCK_RUNNING_HOURS}h (stuck?)",
                    evidence={
                        "task": full,
                        "state": state,
                        "last_run_time": str(t.get("LastRunTime")),
                    },
                )
            )

        overdue = (
            state == "Ready"
            and next_run is not None
            and now - next_run > timedelta(hours=MISSED_GRACE_HOURS)
        )
        if missed > 0 or overdue:
            findings.append(
                Finding(
                    fingerprint=make_fingerprint(full, "missed-runs"),
                    source=SOURCE,
                    severity="medium",
                    title=f"{full}: schedule not keeping up"
                    + (f" ({missed} missed)" if missed else " (next run is in the past)"),
                    evidence={
                        "task": full,
                        "missed_runs": missed,
                        "next_run_time": str(t.get("NextRunTime")),
                        "state": state,
                    },
                )
            )
    return findings


def scan(task_folders: tuple[str, ...], now: datetime | None = None) -> list[Finding]:
    """Query + analyze. Caller handles RuntimeError (e.g. non-Windows hosts)."""
    records = query_tasks(task_folders)
    return analyze(records, now or datetime.now().astimezone())
