"""Config loading and validation tests. Fixtures use synthetic paths only.

Covers ``overwatch.config.load_config()``: the missing-file default path, TOML
parsing of the shipped ``config.example.toml``, full-field parsing, the
``OVERWATCH_CONFIG`` env-var resolution order, and every validation error the
loader raises (non-loopback host, out-of-range port, non-positive retention,
targets missing a name or a repo/log_dir, duplicate target names, malformed
TOML). All target ``repo``/``log_dir`` values used here are fictitious strings
-- ``Config`` only stores them as ``Path`` objects at this layer and never
touches the filesystem for them, so nothing here needs to exist on disk.
"""

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
    """Write ``body`` as ``tmp_path/config.toml`` and return its path.

    Centralizes the synthetic-config-file pattern every test below uses so
    each test only has to state the TOML fragment it cares about.
    """
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_file_yields_safe_defaults(tmp_path: Path) -> None:
    """A nonexistent config path must load fail-open to safe defaults.

    Safe here means dry-run stays on and no targets are watched -- the
    absence of a config file must never be read as "watch everything".
    """
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg == Config()
    assert cfg.dry_run is True
    assert cfg.targets == ()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT
    assert cfg.retention_days == DEFAULT_RETENTION_DAYS


def test_example_config_loads_and_validates() -> None:
    """The ``config.example.toml`` shipped in the repo must itself be valid.

    This is the file new operators copy from, so it doubles as a template
    smoke test: if it stops parsing or fails validation, onboarding breaks.
    """
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
    """Every top-level and ``[server]``/``[[targets]]`` field round-trips.

    Exercises the non-default path through every field ``load_config`` knows
    about at once, including the derived ``db_path``/``transcripts_dir``
    properties, so a field silently dropped during parsing would show up here.
    """
    # --- Arrange ---
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
    # --- Act ---
    cfg = load_config(p)
    # --- Assert ---
    assert cfg.dry_run is False
    assert cfg.retention_days == 7
    assert cfg.host == "localhost"
    assert cfg.port == 9000
    assert cfg.targets[0].log_glob == "*.txt"
    assert cfg.db_path == Path("rt/data") / "overwatch.db"
    assert cfg.transcripts_dir == Path("rt/data") / "transcripts"


def test_non_loopback_host_refused(tmp_path: Path) -> None:
    """``server.host`` outside the loopback set must be rejected at load time.

    The dashboard has no auth, so this is the one guard standing between a
    misconfigured host entry and an unauthenticated server on the network.
    """
    p = write(tmp_path, '[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p)


def test_bad_port_refused(tmp_path: Path) -> None:
    """A port outside 1-65535 must raise rather than be silently clamped."""
    p = write(tmp_path, "[server]\nport = 70000\n")
    with pytest.raises(ConfigError, match="port"):
        load_config(p)


def test_retention_must_be_positive(tmp_path: Path) -> None:
    """``retention_days = 0`` must be refused, not treated as "purge everything".

    A zero or negative retention window would make the log purger delete
    every file on its next run; the loader must fail closed instead.
    """
    p = write(tmp_path, "retention_days = 0\n")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(p)


def test_target_requires_name(tmp_path: Path) -> None:
    """A target table missing ``name`` must be rejected, not defaulted."""
    p = write(tmp_path, '[[targets]]\nrepo = "C:/x"\n')
    with pytest.raises(ConfigError, match="name"):
        load_config(p)


def test_target_requires_repo_or_log_dir(tmp_path: Path) -> None:
    """A target with neither ``repo`` nor ``log_dir`` set is meaningless.

    Nothing downstream (log scan, log purge, repo hygiene checks) would have
    anything to look at, so the loader refuses it up front.
    """
    p = write(tmp_path, '[[targets]]\nname = "bare"\n')
    with pytest.raises(ConfigError, match="repo.*log_dir|log_dir.*repo"):
        load_config(p)


def test_duplicate_target_names_refused(tmp_path: Path) -> None:
    """Two targets sharing a name must be rejected.

    Target names key issue fingerprints and per-target log-purge audit rows;
    a duplicate would make those records ambiguous.
    """
    p = write(
        tmp_path,
        '[[targets]]\nname = "dup"\nrepo = "C:/a"\n[[targets]]\nname = "dup"\nrepo = "C:/b"\n',
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p)


def test_invalid_toml_reports_path(tmp_path: Path) -> None:
    """A syntax-broken config file must raise ``ConfigError``, not a raw TOML error.

    Wrapping keeps the failure message useful to an operator who has never
    seen ``tomllib``'s own exception type.
    """
    p = write(tmp_path, "this is not toml ===\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p)


def test_env_var_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``OVERWATCH_CONFIG`` must be honored when no explicit path is passed.

    This is the path the collector and dashboard server actually use at
    startup (``load_config()`` with no argument), so it needs its own test
    distinct from the explicit-path cases above.
    """
    p = write(tmp_path, "retention_days = 14\n")
    monkeypatch.setenv("OVERWATCH_CONFIG", str(p))
    cfg = load_config()
    assert cfg.retention_days == 14


@pytest.mark.parametrize(
    "host",
    ["::", "192.168.1.50", "10.0.0.1", "0.0.0.0", "::ffff:127.0.0.1", ""],
)
def test_additional_non_loopback_hosts_refused(tmp_path: Path, host: str) -> None:
    """Every non-loopback bind shape must fail closed, not just ``0.0.0.0``.

    Lock in fail-closed rejection of every non-loopback bind form: IPv6
    any-address, LAN IPs, the v4-mapped-v6 loopback alias, and empty string.
    The dashboard is unauthenticated; a routable bind must never load.
    """
    p = write(tmp_path, f'[server]\nhost = "{host}"\n')
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p)
