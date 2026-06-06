"""Signal (d): host health — disk space, Windows event log criticals,
per-target log freshness.

Platform-specific reads degrade gracefully: on non-Windows hosts (or when a
query fails) the affected check simply contributes nothing. Each check also
returns metric rows for the health board.
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
    """One health-board reading (mirrors store.add_host_health)."""

    metric: str
    value: str
    healthy: bool


def check_disk(path: Path) -> tuple[list[Finding], list[Metric]]:
    """Free-space check on the volume holding ``path``."""
    try:
        usage = shutil.disk_usage(path if path.exists() else path.anchor or ".")
    except OSError:
        return [], []
    free_pct = usage.free / usage.total * 100
    healthy = free_pct >= DISK_WARN_PCT
    metrics = [Metric("disk_free_pct", f"{free_pct:.1f}", healthy)]
    findings: list[Finding] = []
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
    "$e = @(Get-WinEvent -FilterHashtable @{{LogName='System','Application'; Level=1,2; "
    "StartTime=(Get-Date).AddHours(-{hours})}} -ErrorAction SilentlyContinue); "
    "$top = $e | Group-Object ProviderName | Sort-Object Count -Descending "
    "| Select-Object -First 5 | ForEach-Object {{ [PSCustomObject]@{{ "
    "Provider=$_.Name; Count=$_.Count }} }}; "
    "ConvertTo-Json -InputObject @{{ Total = $e.Count; Top = @($top) }} -Depth 3"
)


def check_windows_events(hours: int = WIN_EVENT_HOURS) -> tuple[list[Finding], list[Metric]]:
    """Critical/error events in System+Application over the window. Windows-only."""
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
        data = None
    if data is None:
        return [], []
    return analyze_windows_events(data, hours)


def analyze_windows_events(
    data: dict[str, Any], hours: int = WIN_EVENT_HOURS
) -> tuple[list[Finding], list[Metric]]:
    """Pure analysis of the event summary (fixture-testable)."""
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
    """Flag targets whose schedule went quiet (newest log older than the cap)."""
    if target.max_log_age_hours is None or target.log_dir is None:
        return []
    now = now if now is not None else time.time()
    newest: float | None = None
    if target.log_dir.is_dir():
        for p in target.log_dir.glob(target.log_glob):
            try:
                newest = max(newest or 0.0, p.stat().st_mtime)
            except OSError:
                continue
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
    """Run all host checks; every part degrades to empty on failure."""
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
