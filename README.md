# Claude Overwatch

Real-time browser dashboard for Claude Code — watch tasks and tool calls as they happen.

## What it shows

- **Task Board** — kanban view of Claude's task list (in progress, queued, done)
- **Live Feed** — scrolling stream of every tool call (Read, Edit, Bash, Agent, etc.) with timing

## Prerequisites

- Python 3.11+
- pip

## Install

```powershell
cd C:\path\to\claude-overwatch
pip install -r requirements.txt
```

## Configure hooks

Run once to wire Claude Code's hook system to the dashboard:

```powershell
.\hooks\install.ps1
```

This appends hook entries to `~/.claude/settings.json`. Existing hooks are preserved.

## Run

```powershell
.\start.ps1
```

Then open **http://localhost:8765** in your browser.

## Verify

With the server running, send a test event:

```powershell
Invoke-RestMethod -Uri http://localhost:8765/event -Method POST -ContentType "application/json" `
  -Body '{"phase":"pre","tool_name":"Read","tool_input":{"file_path":"test.py"},"tool_response":{}}'
```

You should see a row appear in the live feed instantly.

## How it works

Claude Code fires shell hooks before and after each tool call. The hook script (`hooks/capture.py`) reads the event JSON from stdin and POSTs it to the local FastAPI server. The server buffers the last 500 events and broadcasts to all connected browsers via WebSocket. The browser dashboard auto-reconnects if the server restarts.

The hook is non-blocking — if the server isn't running, it silently exits without interrupting Claude.

## Security

The server binds to **127.0.0.1** (loopback) only, so the dashboard is reachable just from this
machine. This matters because `/event` and `/ws` are **unauthenticated by design** for local use:
anyone who can reach the port can POST forged events *and* read the buffered stream of your Claude
Code activity (file paths, tool-call arguments). **Do not change the bind to `0.0.0.0` or expose the
port to a network without first adding authentication** (e.g. a shared bearer token the hook also
sends, and/or a reverse proxy). The capture hook only ever talks to `http://localhost:8765`.
