"""SMTP email notification sender (stdlib only — no third-party mailer).

Called by :func:`overwatch.notify.send` when ``cfg.email`` is enabled; depends
only on :mod:`smtplib` and :mod:`email` from the standard library and on
:class:`overwatch.config.SmtpConfig` for connection details.
"""

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

    # Treat an incomplete config as "not set up" rather than an error: a
    # fresh install with cfg.email left on but no SMTP block filled in
    # should log and move on, not raise and take down the AAR run.
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
            # STARTTLS upgrades the plaintext connection before any
            # credential is sent; this assumes the submission port (587,
            # SmtpConfig's default), not the implicit-TLS port (465).
            server.starttls()
            server.login(smtp_cfg.user, smtp_cfg.password)
            server.sendmail(smtp_cfg.from_addr, [to], msg.as_string())
        print(f"[notify/email] sent to {to}")
    except smtplib.SMTPException as exc:
        # Narrowed to SMTPException (not a bare Exception) so a bug in this
        # function itself still surfaces instead of being logged and
        # swallowed like a genuine delivery failure.
        print(f"[notify/email] failed: {exc}")
