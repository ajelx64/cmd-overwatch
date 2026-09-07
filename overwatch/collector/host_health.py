"""Signal (d): host health — disk space, Windows event log criticals,
per-target log freshness.

Platform-specific reads degrade gracefully: on non-Windows hosts (or when a
query fails) the affected check simply contributes nothing. Each check also
returns metric rows for the health board.

Invoked by the collector entrypoint (``overwatch.collector.__main__``) with the
configured targets and the collector's own data directory (used as the disk
whose free space is checked). Depends on ``overwatch.config.Target`` and on
``overwatch.detect.rules`` for the shared ``Finding``/fingerprint vocabulary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from overwatch.config import Target
from overwatch.detect.rules import Finding, make_fingerprint

SOURCE = "host_health"

DISK_CRITICAL_PCT = 10.0
DISK_WARN_PCT = 15.0
WIN_EVENT_HOURS = 48


@dataclass(frozen=True)
class Metric:
    """One health-board reading (mirrors store.add_host_health).

    Attributes:
        metric: Short machine name for the reading, e.g. ``"disk_free_pct"``.
        value: The reading rendered as a string (the health board is
            display-only; it does not need typed values).
        healthy: Whether this reading is within the healthy range.
    """

    metric: str
    value: str
    healthy: bool


def check_disk(path: Path) -> tuple[list[Finding], list[Metric]]:
    """Free-space check on the volume holding ``path``.

    Args:
        path: Any path on the volume to check — it does not need to exist;
            only which volume it would live on matters.

    Returns:
        A tuple of (findings, metrics). ``findings`` is empty unless free
        space is below :data:`DISK_WARN_PCT`; ``metrics`` always has exactly
        one entry when the disk could be read, or is empty if it could not.
    """
    # --- Step 1: read free/total bytes for the volume ---
    try:
        # path may not exist yet (e.g. a data dir not yet created); fall back
        # to its drive/root so the check still resolves to a real volume.
        usage = shutil.disk_usage(path if path.exists() else path.anchor or ".")
    except OSError:
        return [], []

    # --- Step 2: always report the metric, healthy or not ---
    free_pct = usage.free / usage.total * 100
    healthy = free_pct >= DISK_WARN_PCT
    metrics = [Metric("disk_free_pct", f"{free_pct:.1f}", healthy)]
    findings: list[Finding] = []

    # --- Step 3: only raise a finding once free space crosses a threshold ---
    if free_pct < DISK_CRITICAL_PCT:
        severity = "critical"
    elif free_pct < DISK_WARN_PCT:
        severity = "medium"
    else:
        return findings, metrics
    findings.append(
        Finding(
            fingerprint=make_fingerprint("host", "disk-low"),
            source=SOURCE,
            severity=severity,
            title=f"host: disk free {free_pct:.1f}% (threshold {DISK_WARN_PCT:.0f}%)",
            evidence={
                "free_pct": round(free_pct, 1),
                "free_gb": round(usage.free / 1e9, 1),
                "total_gb": round(usage.total / 1e9, 1),
            },
        )
    )
    return findings, metrics


_WINEVENT_QUERY = (
    # Level 1 = Critical, Level 2 = Error in the Windows event schema — Warning
    # (3) and below are intentionally excluded to keep this signal high-value.
    "$e = @(Get-WinEvent -FilterHashtable @{{LogName='System','Application'; Level=1,2; "
    "StartTime=(Get-Date).AddHours(-{hours})}} -ErrorAction SilentlyContinue); "
    "$top = $e | Group-Object ProviderName | Sort-Object Count -Descending "
    "| Select-Object -First 5 | ForEach-Object {{ [PSCustomObject]@{{ "
    "Provider=$_.Name; Count=$_.Count }} }}; "
    "ConvertTo-Json -InputObject @{{ Total = $e.Count; Top = @($top) }} -Depth 3"
)


def check_windows_events(hours: int = WIN_EVENT_HOURS) -> tuple[list[Finding], list[Metric]]:
    """Critical/error events in System+Application over the window. Windows-only.

    Args:
        hours: Lookback window in hours.

    Returns:
        The result of :func:`analyze_windows_events`, or ``([], [])`` on any
        non-Windows host or whenever the query could not be run or parsed —
        this signal never raises, it only ever contributes nothing.
    """
    if sys.platform != "win32":
        return [], []
    exe = shutil.which("pwsh") or "powershell"
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _WINEVENT_QUERY.format(hours=hours)],
            capture_output=True,
            text=True,
            timeout=90,
        )
        out = proc.stdout.strip()
        data = json.loads(out) if proc.returncode == 0 and out else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        # Missing PowerShell, a slow host log blowing the timeout, or
        # malformed JSON all degrade the same way: no event-log signal this
        # pass, rather than failing the whole collector run.
        data = None
    if data is None:
        return [], []
    return analyze_windows_events(data, hours)


def analyze_windows_events(
    data: dict[str, Any], hours: int = WIN_EVENT_HOURS
) -> tuple[list[Finding], list[Metric]]:
    """Pure analysis of the event summary (fixture-testable).

    Args:
        data: Parsed JSON from ``_WINEVENT_QUERY``: ``{"Total": int, "Top":
            [{"Provider": str, "Count": int}, ...]}``.
        hours: The lookback window used to produce ``data``, echoed into the
            metric name and finding evidence for context.

    Returns:
        Empty findings with a healthy metric when ``Total`` is zero;
        otherwise one medium-severity finding summarizing the top noisiest
        providers, plus the same metric marked unhealthy.
    """
    total = int(data.get("Total") or 0)
    top = data.get("Top") or []
    metrics = [Metric(f"win_event_criticals_{hours}h", str(total), total == 0)]
    if total == 0:
        return [], metrics
    top_desc = ", ".join(
        f"{t.get('Provider')}×{t.get('Count')}" for t in top if isinstance(t, dict)
    )
    findings = [
        Finding(
            fingerprint=make_fingerprint("host", "win-event-criticals"),
            source=SOURCE,
            severity="medium",
            title=f"host: {total} critical/error Windows event(s) in {hours}h",
            evidence={"total": total, "window_hours": hours, "top_providers": top_desc},
        )
    ]
    return findings, metrics


def check_log_freshness(target: Target, now: float | None = None) -> list[Finding]:
    """Flag targets whose schedule went quiet (newest log older than the cap).

    Args:
        target: Target configuration; this check is opt-in per target via
            ``max_log_age_hours`` — targets that leave it unset are skipped.
        now: Reference epoch time; defaults to the current time. Overridable
            so tests don't depend on the wall clock.

    Returns:
        A single high-severity finding if the newest matching log is older
        than the configured cap (or no matching log exists at all), else an
        empty list.
    """
    # --- Step 1: this check only applies to targets that opted in ---
    if target.max_log_age_hours is None or target.log_dir is None:
        return []
    now = now if now is not None else time.time()

    # --- Step 2: find the newest matching log file's mtime, if any ---
    newest: float | None = None
    if target.log_dir.is_dir():
        for p in target.log_dir.glob(target.log_glob):
            try:
                newest = max(newest or 0.0, p.stat().st_mtime)
            except OSError:
                # File removed between glob() and stat() (e.g. a concurrent
                # purge) — skip it rather than fail the whole check.
                continue

    # --- Step 3: age_h is None when no log exists at all; treat that as stale too ---
    age_h = None if newest is None else (now - newest) / 3600
    if age_h is not None and age_h <= target.max_log_age_hours:
        return []
    desc = "no logs found" if age_h is None else f"newest log is {age_h:.0f}h old"
    return [
        Finding(
            fingerprint=make_fingerprint(target.name, "logs-stale"),
            source=SOURCE,
            severity="high",
            title=f"{target.name}: schedule looks dead — {desc} "
            f"(cap {target.max_log_age_hours}h)",
            evidence={
                "target": target.name,
                "max_log_age_hours": target.max_log_age_hours,
                "newest_log_age_hours": None if age_h is None else round(age_h, 1),
            },
        )
    ]


def scan(
    targets: tuple[Target, ...], data_dir: Path
) -> tuple[list[Finding], list[Metric]]:
    """Run all host checks; every part degrades to empty on failure.

    Single flat pass: run each independent check and accumulate its
    findings/metrics. The checks don't depend on each other's results, so
    this is one dispatch sequence rather than staged phases.

    Args:
        targets: Configured targets, used for the per-target log-freshness
            check.
        data_dir: The collector's data directory; its volume is what
            :func:`check_disk` reports on.

    Returns:
        Combined findings and metrics from disk, Windows-event, and
        per-target log-freshness checks.
    """
    findings: list[Finding] = []
    metrics: list[Metric] = []

    f, m = check_disk(data_dir)
    findings += f
    metrics += m

    f, m = check_windows_events()
    findings += f
    metrics += m

    for target in targets:
        findings += check_log_freshness(target)
    return findings, metrics
