#!/usr/bin/env python3
"""Claude Code hook: captures tool events and POSTs to Claude Overwatch server."""

import json
import sys


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    try:
        import httpx

        # Read the full hook payload from stdin
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}

        # Build the event payload for the server
        event = {
            "phase": phase,
            "tool_name": payload.get("tool_name", "unknown"),
            "tool_input": payload.get("tool_input", {}),
            "tool_response": payload.get("tool_response", {}),
        }

        # POST to Claude Overwatch server (non-blocking: 500ms timeout)
        httpx.post(
            "http://localhost:8765/event",
            json=event,
            timeout=0.5,
        )
    except Exception:
        # CRITICAL: never block Claude Code, always exit 0
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
