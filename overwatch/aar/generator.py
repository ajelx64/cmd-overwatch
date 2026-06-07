"""Daily After-Action Report (AAR): the operator's morning brief.

One markdown file per day under ``reports/daily/``; re-runs append a
timestamped ``## Re-run`` section instead of overwriting (the day's story
accumulates). Every generation is recorded in ``aar_records`` so the
dashboard can serve the latest brief.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from overwatch.config import Config
from overwatch.store import Store

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_ACTIVE = ("open", "drafted", "pending_approval", "executing")


def _tiles(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_target: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue["status"] not in _ACTIVE:
            continue
        target = str((issue.get("evidence") or {}).get("target") or issue["source"])
        tile = by_target.setdefault(
            target, {"target": target, "active": 0, "worst": "low"}
        )
        tile["active"] += 1
        if _SEV_ORDER[issue["severity"]] < _SEV_ORDER[tile["worst"]]:
            tile["worst"] = issue["severity"]
    return sorted(by_target.values(), key=lambda t: _SEV_ORDER[t["worst"]])


def _issue_line(issue: dict[str, Any]) -> str:
    return (
        f"- **[{issue['severity']}]** #{issue['id']} {issue['title']} "
        f"(seen {issue['count']}x, status: {issue['status']})"
    )


def render(store: Store, cfg: Config, report_date: date, now: datetime) -> tuple[str, str]:
    """Render the report body and a one-line summary."""
    day = report_date.isoformat()
    issues = store.list_issues()
    active = [i for i in issues if i["status"] in _ACTIVE]
    opened_today = [i for i in issues if str(i["first_seen"]).startswith(day)]
    resolved_today = [
        i for i in issues if i["status"] == "resolved" and str(i["last_seen"]).startswith(day)
    ]
    failed = [i for i in issues if i["status"] == "failed"]
    pending = store.list_issues(status="pending_approval")
    executing = store.list_issues(status="executing")
    metrics = store.latest_host_health()

    transcripts: list[Path] = []
    if cfg.transcripts_dir.is_dir():
        transcripts = sorted(
            p
            for p in cfg.transcripts_dir.glob("*.log")
            if datetime.fromtimestamp(p.stat().st_mtime).date() == report_date
        )

    purges = [
        r
        for r in store._conn.execute(  # noqa: SLF001 — report query, read-only
            "SELECT * FROM log_purge_runs WHERE created_at LIKE ? ORDER BY id", (f"{day}%",)
        ).fetchall()
    ]

    lines: list[str] = [
        f"# After-Action Report — {day}",
        "",
        f"Generated {now.strftime('%H:%M %Z')} · mode: "
        + ("**DRY-RUN** (no live action)" if cfg.dry_run else "LIVE"),
        "",
        "## Health board",
        "",
    ]
    tiles = _tiles(issues)
    if tiles:
        lines += [
            f"- **{t['target']}** — {t['active']} active issue(s), worst: {t['worst']}"
            for t in tiles
        ]
    else:
        lines.append("- All targets green: no active issues.")
    if metrics:
        lines += ["", "### Host metrics", ""]
        lines += [
            f"- {m['metric']}: {m['value']} {'✅' if m['healthy'] else '⚠️'}" for m in metrics
        ]

    lines += ["", f"## Issues opened today ({len(opened_today)})", ""]
    lines += [_issue_line(i) for i in opened_today] or ["- none"]

    lines += ["", f"## Issues resolved today ({len(resolved_today)})", ""]
    lines += [_issue_line(i) for i in resolved_today] or ["- none"]

    lines += ["", f"## Awaiting approval ({len(pending)})", ""]
    for issue in pending:
        sols = store.solutions_for_issue(issue["id"])
        gate = sols[-1]["gate_category"] if sols else "?"
        lines.append(f"- #{issue['id']} {issue['title']} — gate: **{gate}**")
    if not pending:
        lines.append("- none")

    if executing:
        lines += ["", f"## Executing ({len(executing)})", ""]
        lines += [_issue_line(i) for i in executing]
    if failed:
        lines += ["", f"## Failed executions ({len(failed)})", ""]
        lines += [_issue_line(i) for i in failed]

    lines += ["", f"## Agent transcripts today ({len(transcripts)})", ""]
    lines += [f"- `{p.name}`" for p in transcripts] or ["- none"]

    lines += ["", f"## Log purge activity ({len(purges)})", ""]
    lines += [
        f"- {r['target']}: {r['files_deleted']} file(s), {r['bytes_freed']} bytes"
        + (" (dry-run)" if r["dry_run"] else "")
        for r in purges
    ] or ["- none"]

    summary = (
        f"{len(active)} active / {len(pending)} pending approval / "
        f"{len(resolved_today)} resolved today / {len(opened_today)} opened today"
    )
    return "\n".join(lines) + "\n", summary


def generate(
    store: Store,
    cfg: Config,
    report_date: date | None = None,
    now: datetime | None = None,
) -> Path:
    """Render and persist today's AAR; append a Re-run section if it exists."""
    now = now or datetime.now(UTC)
    report_date = report_date or now.date()
    body, summary = render(store, cfg, report_date, now)

    out_dir = cfg.reports_dir / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report_date.isoformat()}-aar.md"

    if path.exists():
        rerun = f"\n---\n\n## Re-run {now.strftime('%H:%M')}\n\n" + body
        path.write_text(path.read_text(encoding="utf-8") + rerun, encoding="utf-8")
    else:
        path.write_text(body, encoding="utf-8")

    store.add_aar_record(report_date.isoformat(), str(path), summary)
    return path
