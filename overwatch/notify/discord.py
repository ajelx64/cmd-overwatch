"""Discord webhook notification sender (stdlib only — no requests/httpx).

Called by :func:`overwatch.notify.send` when ``cfg.discord`` is enabled. Uses
``urllib.request`` instead of a third-party HTTP client to avoid adding a
dependency for a single POST call.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


def send_discord(webhook_url: str, summary: str, report_path: Path, dry_run: bool) -> None:
    """POST an AAR summary to a Discord webhook.

    Args:
        webhook_url: Discord webhook URL. Silently skipped if empty/blank.
        summary: Short text summary to post.
        report_path: Path to the generated report file (name included in message).
        dry_run: When True, log intent but make no HTTP call.
    """
    if dry_run:
        print(f"[notify/discord] DRY-RUN: would send: {summary}")
        return

    # An unset webhook URL means the channel is enabled but not yet
    # configured; skip quietly rather than making a request that would just
    # fail, matching send_email's "not configured, skipping" behavior.
    if not webhook_url or not webhook_url.strip():
        return

    payload = json.dumps(
        {"content": f"{summary}\n\nReport: {report_path.name}"}
    ).encode()

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # S310 (audit url open for scheme) is suppressed because webhook_url
        # is operator-supplied configuration, not attacker-controlled input.
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            _ = resp.read()
        print("[notify/discord] sent")
    except Exception as exc:  # noqa: BLE001
        # Broad by design: any failure here (network, DNS, a non-2xx status
        # raising HTTPError, etc.) must be logged and swallowed, never allowed
        # to crash the AAR run that triggered the notification.
        print(f"[notify/discord] failed: {exc}")
