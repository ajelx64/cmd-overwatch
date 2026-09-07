"""Entry-point coverage for ``python -m overwatch.aar`` (overwatch/aar/__main__).

``_config()`` writes a minimal config.toml pointing data_dir/reports_dir at
tmp_path, so ``main()`` runs end-to-end against a throwaway store and report
directory rather than anything on the real host.
"""

from pathlib import Path

import pytest

from overwatch.aar.__main__ import main


def _config(tmp_path: Path) -> Path:
    """Write a minimal config.toml with data_dir/reports_dir under tmp_path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        f'reports_dir = "{(tmp_path / "reports").as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg


def test_aar_main_generates_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() with an explicit --date exits 0, prints the written-report
    line, and actually creates the dated report file on disk.
    """
    rc = main(["--config", str(_config(tmp_path)), "--date", "2026-01-06"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[aar] written" in out
    assert (tmp_path / "reports" / "daily" / "2026-01-06-aar.md").exists()


def test_aar_main_notify_flag_is_safe_under_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--notify runs the notification-dispatch branch without raising or
    actually sending anything, given the default (disabled) notify channels
    and dry_run left on by the config fixture.
    """
    # --notify with channels disabled (the defaults) and dry_run on is a no-op:
    # it exercises the notify dispatch branch without sending anything.
    rc = main(["--config", str(_config(tmp_path)), "--date", "2026-01-06", "--notify"])
    assert rc == 0
    assert "[aar] written" in capsys.readouterr().out
