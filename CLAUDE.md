# CLAUDE.md — overwatch (division deltas)

Follows the **Command canonical ruleset** at `guild/master/CLAUDE.md` (approval gates,
branch/commit naming, Review Gate, DRE). This file holds only deltas.

A real-time dashboard that watches Claude Code tasks and tool calls. The repository is
**public** (`ajelx64/claude-overwatch`) — no cross-division IP, paths, or secrets in tracked
files.

## Commands (run in PowerShell, from this division)

```powershell
.\start.ps1            # serves the dashboard at http://localhost:8765
.\hooks\install.ps1    # one-time: wires Claude Code hooks into ~/.claude/settings.json
```

Standard `venv` / `pip install -r requirements.txt` / `pytest` apply.

## Gotcha

`hooks/capture.py` is **non-blocking by design** — if the server is down it exits silently
rather than failing the tool call. A hook that appears to do nothing is the intended
behavior, not a broken install; check whether the server is running before debugging it.
