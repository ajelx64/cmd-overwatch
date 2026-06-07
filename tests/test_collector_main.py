"""Collector entrypoint tests: signal selection, dry-run, persistence, resilience."""

from pathlib import Path

import pytest

from overwatch.collector.__main__ import main, run_signals
from overwatch.config import Config
from overwatch.store import Store

FAILED_EXIT = """\
=== 2026-01-05 07:00:01 -07:00  start sync-job  (python sync.py) ===
fatal: could not resolve host
=== exit 1 @ 07:00:05 ===
"""


def make_config(tmp_path: Path) -> tuple[Config, Path]:
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
    cfg, _ = make_config(tmp_path)
    findings, metrics = run_signals(cfg, {"logs"})
    assert len(findings) == 1
    assert findings[0].source == "log_scan"
    assert metrics == []


def test_run_signals_host_emits_metrics(tmp_path: Path) -> None:
    cfg, _ = make_config(tmp_path)
    _, metrics = run_signals(cfg, {"host"})
    assert any(m.metric == "disk_free_pct" for m in metrics)


def test_broken_signal_becomes_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    cfg, cfg_file = make_config(tmp_path)
    rc = main(["--only", "logs", "--dry-run", "--config", str(cfg_file)])
    assert rc == 0
    assert not cfg.db_path.exists()


def test_main_persists_issues(tmp_path: Path) -> None:
    cfg, cfg_file = make_config(tmp_path)
    rc = main(["--only", "logs", "--config", str(cfg_file)])
    assert rc == 0
    store = Store(cfg.db_path)
    issues = store.list_issues(status="open")
    assert len(issues) == 1
    assert issues[0]["source"] == "log_scan"
    store.close()


def test_main_rerun_dedupes(tmp_path: Path) -> None:
    cfg, cfg_file = make_config(tmp_path)
    main(["--only", "logs", "--config", str(cfg_file)])
    main(["--only", "logs", "--config", str(cfg_file)])
    store = Store(cfg.db_path)
    issues = store.list_issues()
    assert len(issues) == 1
    assert issues[0]["count"] == 2
    store.close()


def test_git_signal_skips_targets_without_repo(tmp_path: Path) -> None:
    cfg, _ = make_config(tmp_path)
    findings, _ = run_signals(cfg, {"git"})
    assert findings == []  # synthetic target has log_dir only


def test_empty_config_runs_clean(tmp_path: Path) -> None:
    empty = tmp_path / "none.toml"
    rc = main(["--only", "logs", "git", "--dry-run", "--config", str(empty)])
    assert rc == 0
