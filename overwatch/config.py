"""Configuration loading and validation.

All machine-specific values (watched repos, log directories, scheduled-task
folders) live in a local ``config.toml`` that is gitignored. The repository
ships only ``config.example.toml``. With no config file present, overwatch
runs with safe defaults and an empty target list.

Security invariants enforced here, not just documented:

- ``server.host`` must be a loopback address. The dashboard has no
  authentication; binding anywhere else is refused at load time.
- ``dry_run`` defaults to ``True``. Live action (executing solutions,
  purging logs, sending notifications) requires the operator to set
  ``dry_run = false`` explicitly in their local config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RETENTION_DAYS = 30
DEFAULT_LOG_GLOB = "*.log"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

ENV_CONFIG_PATH = "OVERWATCH_CONFIG"


class ConfigError(ValueError):
    """Raised when a config file is present but invalid."""


@dataclass(frozen=True)
class Target:
    """One watched project: a repo to check hygiene on and/or a log dir to scan."""

    name: str
    repo: Path | None = None
    log_dir: Path | None = None
    log_glob: str = DEFAULT_LOG_GLOB


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    targets: tuple[Target, ...] = ()
    task_folders: tuple[str, ...] = ()
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    retention_days: int = DEFAULT_RETENTION_DAYS
    dry_run: bool = True
    data_dir: Path = field(default_factory=lambda: Path("data"))
    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    extra_gate_patterns: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "overwatch.db"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"


def _parse_target(raw: dict[str, object], index: int) -> Target:
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"targets[{index}]: 'name' is required and must be a non-empty string")
    repo = raw.get("repo")
    log_dir = raw.get("log_dir")
    if repo is None and log_dir is None:
        raise ConfigError(f"targets[{index}] ({name!r}): set at least one of 'repo' or 'log_dir'")
    log_glob = raw.get("log_glob", DEFAULT_LOG_GLOB)
    if not isinstance(log_glob, str) or not log_glob.strip():
        raise ConfigError(f"targets[{index}] ({name!r}): 'log_glob' must be a non-empty string")
    return Target(
        name=name.strip(),
        repo=Path(str(repo)) if repo is not None else None,
        log_dir=Path(str(log_dir)) if log_dir is not None else None,
        log_glob=log_glob,
    )


def _validate(cfg: Config) -> Config:
    if cfg.host not in _LOOPBACK_HOSTS:
        raise ConfigError(
            f"server.host must be a loopback address ({', '.join(sorted(_LOOPBACK_HOSTS))}); "
            f"got {cfg.host!r}. The dashboard is unauthenticated by design and must not be "
            "exposed beyond this machine."
        )
    if not (1 <= cfg.port <= 65535):
        raise ConfigError(f"server.port must be 1-65535; got {cfg.port}")
    if cfg.retention_days < 1:
        raise ConfigError(f"retention_days must be >= 1; got {cfg.retention_days}")
    seen: set[str] = set()
    for t in cfg.targets:
        if t.name in seen:
            raise ConfigError(f"duplicate target name {t.name!r}")
        seen.add(t.name)
    return cfg


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate configuration.

    Resolution order: explicit ``path`` argument, then the ``OVERWATCH_CONFIG``
    environment variable, then ``./config.toml``. A missing file yields safe
    defaults (empty target list, dry-run on); a present-but-invalid file
    raises :class:`ConfigError`.
    """
    if path is None:
        env_path = os.environ.get(ENV_CONFIG_PATH)
        path = Path(env_path) if env_path else Path("config.toml")
    else:
        path = Path(path)

    if not path.exists():
        return _validate(Config())

    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    server = raw.get("server", {})
    if not isinstance(server, dict):
        raise ConfigError(f"{path}: [server] must be a table")
    raw_targets = raw.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ConfigError(f"{path}: [[targets]] must be an array of tables")
    gates = raw.get("gates", {})
    if not isinstance(gates, dict):
        raise ConfigError(f"{path}: [gates] must be a table")

    targets = tuple(_parse_target(t, i) for i, t in enumerate(raw_targets))
    task_folders = tuple(str(f) for f in raw.get("task_folders", []))
    extra_gate_patterns = tuple(str(p) for p in gates.get("extra_patterns", []))

    cfg = Config(
        targets=targets,
        task_folders=task_folders,
        host=str(server.get("host", DEFAULT_HOST)),
        port=int(server.get("port", DEFAULT_PORT)),
        retention_days=int(raw.get("retention_days", DEFAULT_RETENTION_DAYS)),
        dry_run=bool(raw.get("dry_run", True)),
        data_dir=Path(str(raw.get("data_dir", "data"))),
        reports_dir=Path(str(raw.get("reports_dir", "reports"))),
        extra_gate_patterns=extra_gate_patterns,
    )
    return _validate(cfg)
