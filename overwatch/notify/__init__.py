"""Notification senders: Discord webhook and SMTP email.

Public API: ``send(cfg, summary, report_path, dry_run)``

Failures are non-fatal — every exception is caught and logged to stdout.

Called by ``overwatch.aar.__main__`` after generating a report, when invoked
with ``--notify``. Delegates the actual transport to
:mod:`overwatch.notify.discord` and :mod:`overwatch.notify.email`, imported
lazily below so a channel that isn't configured never pulls in its
dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path

from overwatch.config import NotifyConfig


def send(cfg: NotifyConfig, summary: str, report_path: Path, dry_run: bool) -> None:
    """Send the AAR summary to configured channels. Failures are non-fatal.

    Each channel is attempted independently and wrapped in its own
    try/except: a Discord outage should not prevent the email from going
    out (and vice versa), and neither should ever propagate and crash the
    calling AAR CLI run — the report itself was already written to disk.

    Args:
        cfg: Which channels are enabled (``cfg.discord`` / ``cfg.email``) and
            the SMTP settings — ``cfg.smtp.password`` may itself hold a secret
            read from ``config.toml`` (see the TODO on
            :class:`overwatch.config.NotifyConfig`), not just a non-secret shape.
        summary: Short text to deliver (already redacted by the caller).
        report_path: Path to the generated report; only its filename is sent.
        dry_run: When True, propagated to each sender to log intent without
            making a live network call.
    """
    # --- Step 1: Discord webhook ---
    if cfg.discord:
        from overwatch.notify.discord import send_discord

        webhook_url = os.environ.get("OVERWATCH_DISCORD_WEBHOOK", "")
        try:
            send_discord(webhook_url, summary, report_path, dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[notify] discord error: {exc}")

    # --- Step 2: SMTP email ---
    if cfg.email:
        from overwatch.notify.email import send_email

        smtp_cfg = cfg.smtp
        # Allow env var to override password
        smtp_password = os.environ.get("OVERWATCH_SMTP_PASSWORD", smtp_cfg.password)
        from overwatch.config import SmtpConfig

        # SmtpConfig is frozen, so mutating cfg.smtp in place was never an option;
        # a fresh instance is built here so the env-var override stays local to
        # this one send instead of leaking a resolved password back into the
        # long-lived config object that other callers keep a reference to.
        effective_smtp = SmtpConfig(
            host=smtp_cfg.host,
            port=smtp_cfg.port,
            user=smtp_cfg.user,
            password=smtp_password,
            from_addr=smtp_cfg.from_addr,
            to_addr=smtp_cfg.to_addr,
        )
        subject = f"[overwatch] AAR: {report_path.name}"
        try:
            send_email(effective_smtp, subject, summary, dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[notify] email error: {exc}")
