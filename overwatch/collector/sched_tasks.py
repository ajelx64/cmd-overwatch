"""Signal (a): Windows Task Scheduler health for configured task folders.

The PowerShell query is isolated in :func:`query_tasks`; :func:`analyze` is a
pure function over the resulting records so it stays fixture-testable on any
platform (CI runs on Linux).

Detected:

- ``LastTaskResult`` is a real failure code -> high
- task ``Disabled``                         -> medium
- ``Running`` far longer than expected      -> medium (stuck)
- missed runs / next-run drifted into past  -> medium

Invoked by the collector entrypoint (``overwatch.collector.__main__``) with the
configured task folders. Depends on ``overwatch.detect.rules`` for the shared
``Finding``/fingerprint vocabulary and on the ``Get-ScheduledTask`` /
``Get-ScheduledTaskInfo`` PowerShell cmdlets being available on the host.
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
    """Pick which PowerShell binary to invoke.

    Returns:
        ``"pwsh"`` if PowerShell 7 is on PATH, otherwise the literal string
        ``"powershell"`` to fall back to the Windows PowerShell 5.1 that
        ships with every Windows host.
    """
    import shutil

    return shutil.which("pwsh") or "powershell"


def query_tasks(task_folders: tuple[str, ...]) -> list[dict[str, Any]]:
    """Query Task Scheduler via PowerShell. Windows-only; raises on failure.

    Args:
        task_folders: Task Scheduler folder paths to query, e.g. ``"\\MyApp"``.
            Empty input short-circuits to an empty result without shelling
            out at all.

    Returns:
        One dict per scheduled task, shaped like the ``_PS_QUERY`` projection
        (``TaskPath``, ``TaskName``, ``State``, ``LastRunTime``,
        ``LastTaskResult``, ``NextRunTime``, ``MissedRuns``).

    Raises:
        RuntimeError: If the PowerShell process exits non-zero (e.g. the
            cmdlet isn't available on a non-Windows host, or a folder name is
            invalid).
        json.JSONDecodeError: If PowerShell's ``ConvertTo-Json`` output is not
            valid JSON — not expected in practice, so it is left uncaught.
    """
    if not task_folders:
        return []
    # --- Step 1: build the PowerShell command for the requested folders ---
    # Quote each folder as a PowerShell string literal so the -TaskPath array
    # argument is well-formed even when a folder name contains spaces.
    paths = ",".join(f"'{f}'" for f in task_folders)
    cmd = _PS_QUERY.format(paths=paths)
    # --- Step 2: run it and fail loudly on a non-zero exit ---
    proc = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Get-ScheduledTask query failed: {proc.stderr.strip()[:200]}")
    # --- Step 3: parse and normalize the JSON output ---
    out = proc.stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    # ConvertTo-Json emits a bare object (not a one-element array) when only
    # one task matches; normalize to a list either way.
    return data if isinstance(data, list) else [data]


def _parse_dt(value: Any) -> datetime | None:
    """Parse one of the query's ISO-8601 timestamp fields.

    Args:
        value: The raw JSON value for a time field; may be ``None``,
            missing, or (defensively) a non-string if the PowerShell shape
            ever changes.

    Returns:
        The parsed datetime, or ``None`` if the field was absent or not a
        valid ISO-8601 string (PowerShell only emits the latter when the
        underlying ``LastRunTime``/``NextRunTime`` was itself unset).
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def analyze(tasks: list[dict[str, Any]], now: datetime) -> list[Finding]:
    """Pure analysis of task records (shape mirrors the PowerShell query).

    Args:
        tasks: Records shaped like :func:`query_tasks`'s return value (or
            hand-built fixtures in tests — this function never shells out).
        now: Reference time for the "stuck" and "overdue" age checks, passed
            in explicitly so tests are deterministic.

    Returns:
        Findings for every task that trips one or more of the four checks
        below; a single task can appear in more than one finding.
    """
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

        # --- Step 1: last run ended in a real failure code -> high ---
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

        # --- Step 2: task is disabled -> medium ---
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

        # --- Step 3: reported Running far longer than a normal run -> medium ---
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

        # --- Step 4: missed runs, or a "Ready" task whose next run already passed -> medium ---
        # A small grace window (MISSED_GRACE_HOURS) absorbs normal scheduler
        # jitter so a next-run time a few minutes in the past isn't flagged.
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
    """Query + analyze. Caller handles RuntimeError (e.g. non-Windows hosts).

    Single flat pass through the two already-documented steps (query, then
    analyze); no further phases to narrate here.

    Args:
        task_folders: Task Scheduler folder paths to query.
        now: Reference time for age-based checks; defaults to the current
            local time (with timezone) when not supplied.

    Returns:
        Findings from :func:`analyze` over the freshly queried tasks.

    Raises:
        RuntimeError: Propagated from :func:`query_tasks` if the PowerShell
            query fails (e.g. this collector was invoked on a non-Windows
            host, where the underlying cmdlet does not exist).
    """
    records = query_tasks(task_folders)
    return analyze(records, now or datetime.now().astimezone())
