"""Config loading and validation tests. Fixtures use synthetic paths only."""

from pathlib import Path

import pytest

from overwatch.config import (
    DEFAULT_PORT,
    DEFAULT_RETENTION_DAYS,
    Config,
    ConfigError,
    load_config,
)


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_file_yields_safe_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg == Config()
    assert cfg.dry_run is True
    assert cfg.targets == ()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT
    assert cfg.retention_days == DEFAULT_RETENTION_DAYS


def test_example_config_loads_and_validates() -> None:
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    cfg = load_config(example)
    assert cfg.dry_run is True
    assert len(cfg.targets) == 2
    assert cfg.targets[0].name == "example-project"
    assert cfg.targets[0].repo is not None
    assert cfg.targets[1].repo is None
    assert cfg.task_folders == ("\\MyAutomation\\",)
    assert cfg.extra_gate_patterns == ("deploy", "terraform")


def test_full_config_parses(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        """
        dry_run = false
        retention_days = 7
        data_dir = "rt/data"
        reports_dir = "rt/reports"
        task_folders = ["\\\\Jobs\\\\"]

        [server]
        host = "localhost"
        port = 9000

        [[targets]]
        name = "alpha"
        repo = "C:/work/alpha"
        log_glob = "*.txt"
        """,
    )
    cfg = load_config(p)
    assert cfg.dry_run is False
    assert cfg.retention_days == 7
    assert cfg.host == "localhost"
    assert cfg.port == 9000
    assert cfg.targets[0].log_glob == "*.txt"
    assert cfg.db_path == Path("rt/data") / "overwatch.db"
    assert cfg.transcripts_dir == Path("rt/data") / "transcripts"


def test_non_loopback_host_refused(tmp_path: Path) -> None:
    p = write(tmp_path, '[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p)


def test_bad_port_refused(tmp_path: Path) -> None:
    p = write(tmp_path, "[server]\nport = 70000\n")
    with pytest.raises(ConfigError, match="port"):
        load_config(p)


def test_retention_must_be_positive(tmp_path: Path) -> None:
    p = write(tmp_path, "retention_days = 0\n")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(p)


def test_target_requires_name(tmp_path: Path) -> None:
    p = write(tmp_path, '[[targets]]\nrepo = "C:/x"\n')
    with pytest.raises(ConfigError, match="name"):
        load_config(p)


def test_target_requires_repo_or_log_dir(tmp_path: Path) -> None:
    p = write(tmp_path, '[[targets]]\nname = "bare"\n')
    with pytest.raises(ConfigError, match="repo.*log_dir|log_dir.*repo"):
        load_config(p)


def test_duplicate_target_names_refused(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        '[[targets]]\nname = "dup"\nrepo = "C:/a"\n[[targets]]\nname = "dup"\nrepo = "C:/b"\n',
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p)


def test_invalid_toml_reports_path(tmp_path: Path) -> None:
    p = write(tmp_path, "this is not toml ===\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p)


def test_env_var_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = write(tmp_path, "retention_days = 14\n")
    monkeypatch.setenv("OVERWATCH_CONFIG", str(p))
    cfg = load_config()
    assert cfg.retention_days == 14


@pytest.mark.parametrize(
    "host",
    ["::", "192.168.1.50", "10.0.0.1", "0.0.0.0", "::ffff:127.0.0.1", ""],
)
def test_additional_non_loopback_hosts_refused(tmp_path: Path, host: str) -> None:
    # Lock in fail-closed rejection of every non-loopback bind form: IPv6
    # any-address, LAN IPs, the v4-mapped-v6 loopback alias, and empty string.
    # The dashboard is unauthenticated; a routable bind must never load.
    p = write(tmp_path, f'[server]\nhost = "{host}"\n')
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p)
