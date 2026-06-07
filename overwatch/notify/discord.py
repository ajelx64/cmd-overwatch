"""Discord webhook notification sender (stdlib only — no requests/httpx)."""

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
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            _ = resp.read()
        print("[notify/discord] sent")
    except Exception as exc:  # noqa: BLE001
        print(f"[notify/discord] failed: {exc}")
