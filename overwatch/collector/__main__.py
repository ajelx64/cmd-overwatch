"""Collector entrypoint: run detection signals and persist findings.

Usage::

    python -m overwatch.collector                 # all signals
    python -m overwatch.collector --only logs git # subset
    python -m overwatch.collector --dry-run       # print findings, write nothing

Designed to run from Windows Task Scheduler on an interval; the dashboard
server only ever *reads* what this writes (plus its own event/approval
writes), so the health board stays current even when the server is down.
A signal that crashes becomes a ``collector`` self-health finding instead of
aborting the pass.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime

from overwatch.collector import git_hygiene, host_health, log_purge, log_scan, sched_tasks
from overwatch.config import Config, load_config
from overwatch.detect.rules import Finding, make_fingerprint, persist_findings
from overwatch.solution.pipeline import dispatch_solution, draft_new_issues
from overwatch.store import Store

SIGNALS = ("logs", "sched", "git", "host")


def _signal_failed(signal: str, exc: Exception) -> Finding:
    return Finding(
        fingerprint=make_fingerprint("collector", signal, "signal-failed"),
        source="collector",
        severity="medium",
        title=f"collector: signal '{signal}' failed ({type(exc).__name__})",
        evidence={"signal": signal, "error": str(exc)[:300]},
    )


def run_signals(cfg: Config, only: set[str]) -> tuple[list[Finding], list[host_health.Metric]]:
    findings: list[Finding] = []
    metrics: list[host_health.Metric] = []

    runners: dict[str, Callable[[], None]] = {}

    def _logs() -> None:
        for target in cfg.targets:
            findings.extend(log_scan.scan_target(target))

    def _sched() -> None:
        findings.extend(sched_tasks.scan(cfg.task_folders))

    def _git() -> None:
        for target in cfg.targets:
            if target.repo is not None:
                findings.extend(git_hygiene.scan_repo(target.name, target.repo))

    def _host() -> None:
        f, m = host_health.scan(cfg.targets, cfg.data_dir)
        findings.extend(f)
        metrics.extend(m)

    runners = {"logs": _logs, "sched": _sched, "git": _git, "host": _host}
    for signal in SIGNALS:
        if signal not in only:
            continue
        try:
            runners[signal]()
        except Exception as exc:  # one broken signal must not kill the pass
            findings.append(_signal_failed(signal, exc))
    return findings, metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="overwatch.collector", description=__doc__)
    parser.add_argument("--only", nargs="+", choices=SIGNALS, default=list(SIGNALS))
    parser.add_argument(
        "--dry-run", action="store_true", help="print findings without writing to the store"
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    only = set(args.only)

    started = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[collector] pass started {started}; signals: {', '.join(sorted(only))}")

    findings, metrics = run_signals(cfg, only)

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        print(f"  [{f.severity}] {f.title}")
    for m in metrics:
        print(f"  metric {m.metric}={m.value} healthy={m.healthy}")

    if args.dry_run:
        print(f"[collector] dry-run: {len(findings)} finding(s) NOT persisted")
        return 0

    store = Store(cfg.db_path)
    try:
        ids = persist_findings(store, findings)
        for m in metrics:
            store.add_host_health(m.metric, m.value, m.healthy)
        print(f"[collector] persisted {len(ids)} issue(s) ({by_sev or 'none'})")

        log_purge.purge_all(cfg, store)

        drafted = draft_new_issues(store, cfg)
        for issue_id, sid, gated in drafted:
            route = "pending approval" if gated else "auto"
            print(f"[pipeline] issue #{issue_id}: solution #{sid} drafted -> {route}")
            if not gated:
                solution = store.get_solution(sid)
                assert solution is not None
                result = dispatch_solution(store, cfg, solution)
                print(f"[pipeline]   dispatch: {result.status} — {result.detail}")
        pending = len(store.list_issues(status="pending_approval"))
        print(f"[collector] pending approvals: {pending}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
