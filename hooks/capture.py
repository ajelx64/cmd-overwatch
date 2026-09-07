#!/usr/bin/env python3
"""Claude Code hook: captures tool events and POSTs to Claude Overwatch server.

Registered as a PreToolUse/PostToolUse/Stop hook (see ``hooks/install.ps1``),
so this script is invoked directly by Claude Code itself, not imported by
anything in the ``overwatch`` package. It has no dependency on the rest of
this repository — it only needs ``httpx`` and the stdlib — so it keeps
working even if the ``overwatch`` package fails to import.

Non-blocking by design: this must never slow down or fail a tool call, so
every error (including "the server isn't running") is swallowed and the
process always exits 0. See the CLAUDE.md gotcha in this repository's root
for why a hook that appears to do nothing is not itself a bug.
"""

import json
import sys


def main() -> None:
    """Read one hook payload from stdin and best-effort POST it to the server.

    Never raises and never sets a nonzero exit code — see the module
    docstring for why that's a hard requirement here, not a style choice.
    """
    # --- Step 1: identify which hook phase invoked this run ---
    phase = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    # --- Step 2: build and POST the event, swallowing any failure ---
    try:
        # Imported inside the try block so a missing/broken httpx install
        # also falls into the catch-all below instead of crashing the hook.
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
        # 500ms caps how long a tool call can be delayed if the server is up
        # but slow to respond; redaction happens server-side on receipt, not
        # here, so this payload is unredacted in flight to localhost.
        httpx.post(
            "http://localhost:8765/event",
            json=event,
            timeout=0.5,
        )
    except Exception:
        # CRITICAL: never block Claude Code, always exit 0. This is
        # deliberately unconditional — a down server, a malformed stdin
        # payload, and a network hiccup must all be handled identically.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
