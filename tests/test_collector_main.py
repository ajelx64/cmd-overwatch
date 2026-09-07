"""Tests for ``overwatch.collector.__main__``: signal dispatch, dry-run,
persistence into the store, and per-signal failure isolation.

``make_config()`` builds a minimal config.toml plus one on-disk log file
shaped like a failed command run (the FAILED_EXIT fixture text below), which
is what ``log_scan`` is expected to find and flag — so most tests exercise a
real, if synthetic, end-to-end failure rather than a mocked Finding.
"""

from pathlib import Path

import pytest

from overwatch.collector.__main__ import main, run_signals
from overwatch.config import Config
from overwatch.store import Store

# Minimal log-scan-recognizable transcript: a start banner, one failure line,
# and a nonzero-exit banner — just enough for log_scan to flag one issue.
FAILED_EXIT = """\
=== 2026-01-05 07:00:01 -07:00  start sync-job  (python sync.py) ===
fatal: could not resolve host
=== exit 1 @ 07:00:05 ===
"""


def make_config(tmp_path: Path) -> tuple[Config, Path]:
    """Write one synthetic target (a log dir with a single failed-run log
    file) and its config.toml under tmp_path; shared setup for every test
    below.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "sync-job-2026-01-05.log").write_text(FAILED_EXIT, encoding="utf-8")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
data_dir = "{(tmp_path / "data").as_posix()}"

[[targets]]
name = "synthetic"
log_dir = "{log_dir.as_posix()}"
""",
        encoding="utf-8",
    )
    from overwatch.config import load_config

    return load_config(cfg_file), cfg_file


def test_run_signals_logs_only(tmp_path: Path) -> None:
    """Selecting only the "logs" signal runs log_scan and nothing else: the
    one seeded failure is found, and no metrics are produced (host_health,
    the only metric producer, never ran).
    """
    cfg, _ = make_config(tmp_path)
    findings, metrics = run_signals(cfg, {"logs"})
    assert len(findings) == 1
    assert findings[0].source == "log_scan"
    assert metrics == []


def test_run_signals_host_emits_metrics(tmp_path: Path) -> None:
    """The "host" signal emits a disk_free_pct metric regardless of the
    synthetic target's log-only setup — host_health reads real host state,
    not anything from the config fixture.
    """
    cfg, _ = make_config(tmp_path)
    _, metrics = run_signals(cfg, {"host"})
    assert any(m.metric == "disk_free_pct" for m in metrics)


def test_broken_signal_becomes_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A signal that raises is caught and turned into a "collector"
    self-health finding instead of crashing the whole pass — the module's
    core resilience contract (see its docstring on one broken signal not
    aborting the pass).

    sched_tasks.scan is monkeypatched to raise directly, since forcing the
    real scheduled-tasks read to fail deterministically isn't practical here.
    """
    cfg, _ = make_config(tmp_path)

    def boom(folders: tuple[str, ...]) -> list[object]:
        raise RuntimeError("scheduler unavailable")

    import overwatch.collector.__main__ as entry

    monkeypatch.setattr(entry.sched_tasks, "scan", boom)
    findings, _ = run_signals(cfg, {"sched"})
    assert len(findings) == 1
    assert findings[0].source == "collector"
    assert "sched" in findings[0].title


def test_main_dry_run_writes_nothing(tmp_path: Path) -> None:
    """--dry-run finds the seeded issue (exit 0) without ever creating the
    store's database file — nothing is persisted.
    """
    cfg, cfg_file = make_config(tmp_path)
    rc = main(["--only", "logs", "--dry-run", "--config", str(cfg_file)])
    assert rc == 0
    assert not cfg.db_path.exists()


def test_main_persists_drafts_and_queues(tmp_path: Path) -> None:
    """A full (non-dry-run) pass persists the detected failure as an issue,
    drafts a solution for it, and — since a free-form log-scan fix is never
    on SAFE_KINDS — leaves it pending_approval rather than auto-dispatching.
    """
    cfg, cfg_file = make_config(tmp_path)
    rc = main(["--only", "logs", "--config", str(cfg_file)])
    assert rc == 0
    store = Store(cfg.db_path)
    # the failure was detected, drafted, and (being a free-form fix) gated
    issues = store.list_issues(status="pending_approval")
    assert len(issues) == 1
    assert issues[0]["source"] == "log_scan"
    solutions = store.solutions_for_issue(issues[0]["id"])
    assert len(solutions) == 1
    assert solutions[0]["kind"] == "investigate-fix"
    store.close()


def test_main_rerun_dedupes(tmp_path: Path) -> None:
    """Running the same pass twice against unchanged input upserts the same
    issue (matched by fingerprint) rather than creating a duplicate — the
    second run only bumps its seen-count.
    """
    cfg, cfg_file = make_config(tmp_path)
    main(["--only", "logs", "--config", str(cfg_file)])
    main(["--only", "logs", "--config", str(cfg_file)])
    store = Store(cfg.db_path)
    issues = store.list_issues()
    assert len(issues) == 1
    assert issues[0]["count"] == 2
    store.close()


def test_git_signal_skips_targets_without_repo(tmp_path: Path) -> None:
    """A target with no configured repo path is silently skipped by the git
    signal rather than erroring — most targets in practice are log-only.
    """
    cfg, _ = make_config(tmp_path)
    findings, _ = run_signals(cfg, {"git"})
    assert findings == []  # synthetic target has log_dir only


def test_empty_config_runs_clean(tmp_path: Path) -> None:
    """Pointing --config at a nonexistent file still runs cleanly
    (load_config falls back to defaults), and dry-run exits 0 with no
    targets configured — the collector must not require a config file to
    exist.
    """
    empty = tmp_path / "none.toml"
    rc = main(["--only", "logs", "git", "--dry-run", "--config", str(empty)])
    assert rc == 0
