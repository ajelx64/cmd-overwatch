"""Notification senders: Discord webhook and SMTP email.

Public API: ``send(cfg, summary, report_path, dry_run)``

Failures are non-fatal — every exception is caught and logged to stdout.
"""

from __future__ import annotations

import os
from pathlib import Path

from overwatch.config import NotifyConfig


def send(cfg: NotifyConfig, summary: str, report_path: Path, dry_run: bool) -> None:
    """Send the AAR summary to configured channels. Failures are non-fatal."""
    if cfg.discord:
        from overwatch.notify.discord import send_discord

        webhook_url = os.environ.get("OVERWATCH_DISCORD_WEBHOOK", "")
        try:
            send_discord(webhook_url, summary, report_path, dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[notify] discord error: {exc}")

    if cfg.email:
        from overwatch.notify.email import send_email

        smtp_cfg = cfg.smtp
        # Allow env var to override password
        smtp_password = os.environ.get("OVERWATCH_SMTP_PASSWORD", smtp_cfg.password)
        from overwatch.config import SmtpConfig

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
