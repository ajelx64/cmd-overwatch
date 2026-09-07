"""Configuration loading and validation.

All machine-specific values (watched repos, log directories, scheduled-task
folders) live in a local ``config.toml`` that is gitignored. The repository
ships only ``config.example.toml``. With no config file present, overwatch
runs with safe defaults and an empty target list.

``load_config()`` is the sole entry point: the dashboard server and the
collector each call it once at startup to get a validated, immutable
:class:`Config`. This module depends only on the standard library
(``tomllib`` for parsing, ``dataclasses`` for the result types) — it has no
knowledge of the store, redaction, or the HTTP layer.

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
class SmtpConfig:
    """SMTP connection settings, as loaded from ``[notify.smtp]`` in config.toml.

    Attributes:
        host: SMTP server hostname.
        port: SMTP server port.
        user: SMTP auth username.
        password: SMTP auth password. May be set directly in
            ``[notify.smtp]`` in config.toml — ``_parse_notify`` below reads
            it straight from that table (``config.py`` ~line 238) — and is
            overridden at send time by ``overwatch.notify.send`` with the
            ``OVERWATCH_SMTP_PASSWORD`` environment variable when that is
            set. Config.toml is the fallback, not a forbidden location: a
            password stored there is used by design. See the
            ``TODO(comprehension)`` on :class:`NotifyConfig` below, which
            flags the same env-vs-config reality for this field.
        from_addr: Envelope/header "From" address for outgoing notifications.
        to_addr: Destination address for outgoing notifications.
    """

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""
    to_addr: str = ""


@dataclass(frozen=True)
class NotifyConfig:
    """Notification channel configuration.

    Enable channels with ``discord = true`` / ``email = true`` in ``[notify]``.
    The webhook URL used for the discord channel is intended to be supplied
    via an environment variable at send time, not written to config.toml.

    TODO(comprehension): SmtpConfig.password is intended to work the same
    way, but ``_parse_notify`` below reads "password" straight out of the
    ``[notify.smtp]`` table if present, so nothing here actually stops a
    password from being read from config.toml for that field.

    Attributes:
        discord: Whether the Discord webhook channel is enabled.
        email: Whether the SMTP email channel is enabled.
        smtp: SMTP connection settings for the email channel.
    """

    discord: bool = False
    email: bool = False
    smtp: SmtpConfig = field(default_factory=SmtpConfig)


@dataclass(frozen=True)
class Target:
    """One watched project: a repo to check hygiene on and/or a log dir to scan.

    Attributes:
        name: Unique label for this target (validated by ``_validate``).
        repo: Path to a git repo to check hygiene on, if any.
        log_dir: Path to a directory of logs to scan, if any. ``repo`` and
            ``log_dir`` are independent — a target may set one or both, but
            ``_parse_target`` requires at least one.
        log_glob: Filename glob used to find this target's log files.
        max_log_age_hours: See below.
    """

    name: str
    repo: Path | None = None
    log_dir: Path | None = None
    log_glob: str = DEFAULT_LOG_GLOB
    # If set, raise an issue when the newest matching log is older than this —
    # catches schedules that silently stopped running (e.g. backups).
    max_log_age_hours: int | None = None


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration.

    Returned only by :func:`load_config`, which applies :func:`_validate`
    before handing it back — code elsewhere can treat any ``Config``
    instance as already checked.

    Attributes:
        targets: Watched projects; see :class:`Target`.
        task_folders: Scheduled-task folder names, from ``task_folders`` in
            config.toml. This module only parses the list; what checks it
            against is out of scope here.
        host: Address the dashboard server binds to; loopback-only.
        port: Port the dashboard server listens on.
        retention_days: How long to keep data before it's eligible for
            pruning.
        dry_run: When ``True`` (the default), live/destructive actions are
            simulated rather than executed; see the module docstring.
        data_dir: Directory holding the SQLite database and transcripts.
        reports_dir: Directory AAR/report output is written to.
        extra_gate_patterns: Additional patterns from ``[gates].extra_patterns``
            in config.toml. This module only parses and stores the list; how
            the patterns are matched against issues is out of scope here.
        notify: Notification channel settings; see :class:`NotifyConfig`.
    """

    targets: tuple[Target, ...] = ()
    task_folders: tuple[str, ...] = ()
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    retention_days: int = DEFAULT_RETENTION_DAYS
    dry_run: bool = True
    data_dir: Path = field(default_factory=lambda: Path("data"))
    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    extra_gate_patterns: tuple[str, ...] = ()
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file, derived from ``data_dir``."""
        return self.data_dir / "overwatch.db"

    @property
    def transcripts_dir(self) -> Path:
        """Path to the transcripts directory, derived from ``data_dir``."""
        return self.data_dir / "transcripts"


def _parse_target(raw: dict[str, object], index: int) -> Target:
    """Validate and build one ``[[targets]]`` TOML table into a :class:`Target`.

    Args:
        raw: The parsed TOML table for this target.
        index: Position in the ``targets`` array, used only to make error
            messages point at the offending entry.

    Returns:
        The validated :class:`Target`.

    Raises:
        ConfigError: If any field is missing, the wrong type, or otherwise
            out of range — see the per-field checks below for specifics.
    """
    # --- Step 1: name is mandatory and must be non-empty ---
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"targets[{index}]: 'name' is required and must be a non-empty string")
    # --- Step 2: at least one of repo / log_dir must be set, else there's ---
    # --- nothing for this target to watch ---
    repo = raw.get("repo")
    log_dir = raw.get("log_dir")
    if repo is None and log_dir is None:
        raise ConfigError(f"targets[{index}] ({name!r}): set at least one of 'repo' or 'log_dir'")
    # --- Step 3: log_glob, defaulted but still type/emptiness-checked ---
    # --- since it can be overridden per target ---
    log_glob = raw.get("log_glob", DEFAULT_LOG_GLOB)
    if not isinstance(log_glob, str) or not log_glob.strip():
        raise ConfigError(f"targets[{index}] ({name!r}): 'log_glob' must be a non-empty string")
    # --- Step 4: max_log_age_hours is optional; when present it must be a ---
    # --- positive integer count of hours ---
    max_age = raw.get("max_log_age_hours")
    if max_age is not None and (not isinstance(max_age, int) or max_age < 1):
        raise ConfigError(
            f"targets[{index}] ({name!r}): 'max_log_age_hours' must be a positive integer"
        )
    # --- Step 5: construct the validated Target ---
    return Target(
        name=name.strip(),
        repo=Path(str(repo)) if repo is not None else None,
        log_dir=Path(str(log_dir)) if log_dir is not None else None,
        log_glob=log_glob,
        max_log_age_hours=max_age,
    )


def _parse_notify(raw: object) -> NotifyConfig:
    """Parse the ``[notify]`` TOML table into a :class:`NotifyConfig`.

    A single flat pass: pull the ``smtp`` sub-table (if any) into an
    :class:`SmtpConfig`, then wrap it with the two channel toggles. No
    branching beyond the type guards below, so there's no multi-step
    sequence to narrate.

    Args:
        raw: The value of the top-level ``notify`` key from the parsed TOML,
            or anything else if the key was absent or malformed.

    Returns:
        A :class:`NotifyConfig`. Falls back to all-disabled defaults rather
        than raising if ``raw`` (or its nested ``smtp`` table) isn't a dict —
        ``[notify]`` is an optional section, so a missing or malformed table
        is treated as "no channels enabled" rather than a config error.
    """
    if not isinstance(raw, dict):
        return NotifyConfig()
    smtp_raw = raw.get("smtp", {})
    if not isinstance(smtp_raw, dict):
        smtp_raw = {}
    smtp = SmtpConfig(
        host=str(smtp_raw.get("host", "")),
        port=int(smtp_raw.get("port", 587)),
        user=str(smtp_raw.get("user", "")),
        password=str(smtp_raw.get("password", "")),
        from_addr=str(smtp_raw.get("from_addr", "")),
        to_addr=str(smtp_raw.get("to_addr", "")),
    )
    return NotifyConfig(
        discord=bool(raw.get("discord", False)),
        email=bool(raw.get("email", False)),
        smtp=smtp,
    )


def _validate(cfg: Config) -> Config:
    """Enforce the invariants documented in the module docstring, plus basic sanity checks.

    Runs on every :class:`Config`, whether built from a config.toml or left
    at all defaults (see :func:`load_config`), so a caller can never end up
    with a ``Config`` that skipped these checks.

    Args:
        cfg: The candidate configuration to check.

    Returns:
        ``cfg`` unchanged, if every check passes.

    Raises:
        ConfigError: On the first check that fails.
    """
    # Security invariant, not just a sanity check: the dashboard serves
    # everything with no auth, so binding beyond loopback would expose it to
    # the network.
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
    # ``Target.name`` is meant to be a unique label per the class docstring;
    # a silent duplicate would make two distinct targets indistinguishable
    # to anything that identifies a target by name.
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

    Args:
        path: Explicit path to a config.toml, overriding the environment
            variable and default. Mainly for tests; production callers
            normally pass ``None`` and rely on the environment/default.

    Returns:
        A validated :class:`Config` — either the file's contents, or
        defaults if no file was found.

    Raises:
        ConfigError: If a config file exists but is invalid TOML, has a
            table where a table is required, or fails :func:`_validate`.
    """
    # --- Step 1: resolve which file to read (or that none exists) ---
    if path is None:
        env_path = os.environ.get(ENV_CONFIG_PATH)
        path = Path(env_path) if env_path else Path("config.toml")
    else:
        path = Path(path)

    if not path.exists():
        return _validate(Config())

    # --- Step 2: parse the TOML itself ---
    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    # --- Step 3: type-check the top-level tables before handing their ---
    # --- contents to the per-section parsers, so a mistyped section (e.g. ---
    # --- 'server' as an array) fails with a clear error here rather than ---
    # --- a confusing one further down ---
    server = raw.get("server", {})
    if not isinstance(server, dict):
        raise ConfigError(f"{path}: [server] must be a table")
    raw_targets = raw.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ConfigError(f"{path}: [[targets]] must be an array of tables")
    gates = raw.get("gates", {})
    if not isinstance(gates, dict):
        raise ConfigError(f"{path}: [gates] must be a table")

    # --- Step 4: build the composite/list-valued fields via their own ---
    # --- parsers (targets and notify get dedicated validation; the rest ---
    # --- are simple enough to coerce inline) ---
    targets = tuple(_parse_target(t, i) for i, t in enumerate(raw_targets))
    task_folders = tuple(str(f) for f in raw.get("task_folders", []))
    extra_gate_patterns = tuple(str(p) for p in gates.get("extra_patterns", []))

    notify_cfg = _parse_notify(raw.get("notify", {}))

    # --- Step 5: assemble and validate the final Config ---
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
        notify=notify_cfg,
    )
    return _validate(cfg)
