# Claude Overwatch — Start the dashboard server
# Open http://localhost:8765 in your browser after running this
#
# Binds to 127.0.0.1 (loopback only) on purpose: /event and /ws are unauthenticated, and the
# buffer holds your Claude Code activity (file paths, tool args). Do NOT expose this to a network
# without adding auth first — see SECURITY note in README.

Set-Location $PSScriptRoot
uvicorn server:app --host 127.0.0.1 --port 8765 --reload
