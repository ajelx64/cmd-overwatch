"""Tests for the notify module: Discord webhook and SMTP email senders.

All tests use ``unittest.mock`` — no real HTTP or SMTP connections are made.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from overwatch.config import NotifyConfig, SmtpConfig
from overwatch.notify import send
from overwatch.notify.discord import send_discord
from overwatch.notify.email import send_email

REPORT = Path("reports/2026-06-06-aar.md")
SUMMARY = "0 active issues, 1 resolved — all targets green"
WEBHOOK = "https://discord.com/api/webhooks/123/abc"


# ---------------------------------------------------------------------------
# Discord tests
# ---------------------------------------------------------------------------


def test_discord_dry_run_no_http(capsys: pytest.CaptureFixture[str]) -> None:
    """dry_run=True must print a log line and never call urlopen."""
    with patch("urllib.request.urlopen") as mock_open:
        send_discord(WEBHOOK, SUMMARY, REPORT, dry_run=True)
        mock_open.assert_not_called()
    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
    assert SUMMARY in captured.out


def test_discord_empty_webhook_skips() -> None:
    """Empty/blank webhook URL must silently skip — no HTTP call."""
    with patch("urllib.request.urlopen") as mock_open:
        send_discord("", SUMMARY, REPORT, dry_run=False)
        send_discord("   ", SUMMARY, REPORT, dry_run=False)
        mock_open.assert_not_called()


def test_discord_sends_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Live send must call urlopen with the correct URL and JSON body."""
    import json

    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.read.return_value = b""

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
        send_discord(WEBHOOK, SUMMARY, REPORT, dry_run=False)
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        assert req.full_url == WEBHOOK
        payload = json.loads(req.data.decode())
        assert SUMMARY in payload["content"]
        assert REPORT.name in payload["content"]

    captured = capsys.readouterr()
    assert "sent" in captured.out


def test_discord_failure_non_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    """urlopen raising must not propagate — should print a failure message."""
    with patch("urllib.request.urlopen", side_effect=OSError("network error")):
        # Must not raise
        send_discord(WEBHOOK, SUMMARY, REPORT, dry_run=False)

    captured = capsys.readouterr()
    assert "failed" in captured.out


# ---------------------------------------------------------------------------
# Email tests
# ---------------------------------------------------------------------------


def _full_smtp() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        user="user@example.com",
        password="secret",
        from_addr="user@example.com",
        to_addr="recipient@example.com",
    )


def test_email_dry_run_no_smtp(capsys: pytest.CaptureFixture[str]) -> None:
    """dry_run=True must log intent and never open an SMTP connection."""
    with patch("smtplib.SMTP") as mock_smtp:
        send_email(_full_smtp(), "Subject", "Body", dry_run=True)
        mock_smtp.assert_not_called()

    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
    assert "recipient@example.com" in captured.out


def test_email_missing_config_skips(capsys: pytest.CaptureFixture[str]) -> None:
    """Missing required field (empty host) must skip without SMTP connection."""
    empty_cfg = SmtpConfig()  # all fields empty/defaults
    with patch("smtplib.SMTP") as mock_smtp:
        send_email(empty_cfg, "Subject", "Body", dry_run=False)
        mock_smtp.assert_not_called()

    captured = capsys.readouterr()
    assert "not configured" in captured.out


def test_email_sends(capsys: pytest.CaptureFixture[str]) -> None:
    """Live send must call starttls, login, and sendmail on the SMTP server."""
    mock_server = MagicMock()
    mock_server.__enter__ = lambda s: s
    mock_server.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_server):
        send_email(_full_smtp(), "The Subject", "The Body", dry_run=False)

    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@example.com", "secret")
    mock_server.sendmail.assert_called_once()
    # Verify addressing
    call_args = mock_server.sendmail.call_args
    assert call_args[0][0] == "user@example.com"
    assert "recipient@example.com" in call_args[0][1]

    captured = capsys.readouterr()
    assert "sent" in captured.out


def test_email_failure_non_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    """SMTP raising SMTPException must not propagate — should print a failure message."""
    import smtplib

    mock_server = MagicMock()
    mock_server.__enter__ = lambda s: s
    mock_server.__exit__ = MagicMock(return_value=False)
    mock_server.starttls.side_effect = smtplib.SMTPException("TLS error")

    with patch("smtplib.SMTP", return_value=mock_server):
        # Must not raise
        send_email(_full_smtp(), "Subject", "Body", dry_run=False)

    captured = capsys.readouterr()
    assert "failed" in captured.out


# ---------------------------------------------------------------------------
# Top-level send() tests
# ---------------------------------------------------------------------------


def test_send_disabled_skips_all() -> None:
    """notify.discord=False and notify.email=False → no HTTP, no SMTP."""
    cfg = NotifyConfig(discord=False, email=False)
    with (
        patch("urllib.request.urlopen") as mock_http,
        patch("smtplib.SMTP") as mock_smtp,
    ):
        send(cfg, SUMMARY, REPORT, dry_run=False)
        mock_http.assert_not_called()
        mock_smtp.assert_not_called()


def test_send_dry_run_logs_not_sends(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run=True with both channels enabled → log messages but no actual calls."""
    monkeypatch.setenv("OVERWATCH_DISCORD_WEBHOOK", WEBHOOK)
    cfg = NotifyConfig(discord=True, email=True, smtp=_full_smtp())

    with (
        patch("urllib.request.urlopen") as mock_http,
        patch("smtplib.SMTP") as mock_smtp,
    ):
        send(cfg, SUMMARY, REPORT, dry_run=True)
        mock_http.assert_not_called()
        mock_smtp.assert_not_called()

    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
