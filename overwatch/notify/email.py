"""SMTP email notification sender (stdlib only — no third-party mailer)."""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from overwatch.config import SmtpConfig


def send_email(smtp_cfg: SmtpConfig, subject: str, body: str, dry_run: bool) -> None:
    """Send an AAR summary via SMTP (STARTTLS on port 587).

    Args:
        smtp_cfg: SMTP connection and addressing details.
        subject: Email subject line.
        body: Plain-text body of the message.
        dry_run: When True, log intent but make no SMTP connection.
    """
    to = smtp_cfg.to_addr

    if dry_run:
        print(f"[notify/email] DRY-RUN: would send to {to}: {subject}")
        return

    required = (smtp_cfg.host, smtp_cfg.user, smtp_cfg.password, smtp_cfg.from_addr, to)
    if not all(required):
        print("[notify/email] not configured, skipping")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.from_addr
    msg["To"] = to

    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as server:
            server.starttls()
            server.login(smtp_cfg.user, smtp_cfg.password)
            server.sendmail(smtp_cfg.from_addr, [to], msg.as_string())
        print(f"[notify/email] sent to {to}")
    except smtplib.SMTPException as exc:
        print(f"[notify/email] failed: {exc}")
