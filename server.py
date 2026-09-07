"""The overwatch HTTP/WebSocket server: dashboard backend and hook sink.

This is the FastAPI application that:

- Accepts tool/task/session events from Claude Code hooks (``POST /event``)
  and fans them out live to connected dashboard clients over WebSocket
  (``/ws``), after redacting anything secret-shaped.
- Serves the read-only health-board / issues / approvals API the dashboard
  polls or reads on load.
- Exposes two write paths that can trigger live action: recording an
  operator's approve/deny decision on a drafted solution (which, if approved,
  dispatches to :mod:`overwatch.solution.pipeline`), and the manual
  re-execute endpoint (``POST /api/solutions/{id}/execute``), which calls
  the same dispatch directly and performs no ``pending_approval``/approval
  check of its own — enforcement for a gated solution happens downstream, in
  ``Executor._authorize``, which refuses execution without a recorded
  "approved" decision regardless of which endpoint triggered the call.
- Serves the built dashboard as static files.

Started via ``start.ps1`` (an ASGI runner pointed at ``server:app``); there is
no ``__main__`` block here. Config is loaded once at import time from
``config.toml`` (or ``OVERWATCH_CONFIG``) via :func:`overwatch.config.load_config`,
which also enforces that the server only ever binds to a loopback address —
this API has no authentication of its own and relies entirely on that.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import SessionEvent, TaskEvent, ToolEvent
from overwatch.config import Config, load_config
from overwatch.solution.pipeline import dispatch_solution
from overwatch.store import Store

# There is no real operator identity/session system (single-user, loopback
# only) — this constant stands in for "whoever could reach this endpoint",
# which is only ever the local operator given the loopback-only bind
# enforced in overwatch.config.
DECIDED_BY = "localhost-operator"  # click-as-operator: valid only on 127.0.0.1

config: Config = load_config()
# Module-level singleton rather than app.state: FastAPI route handlers below
# are plain module functions, not methods on a class holding this state.
store: Store | None = None
active_connections: list[WebSocket] = []

# Caps how much history a newly-connected dashboard client is replayed over
# the websocket, so one connection can't be flooded with the entire
# lifetime event log.
REPLAY_LIMIT = 500


def get_store() -> Store:
    """Lazily open the shared store (lets tests point OVERWATCH_CONFIG at a tmp dir).

    Returns:
        The process-wide :class:`~overwatch.store.Store`, opening it against
        ``config.db_path`` on first call.
    """
    global store
    if store is None:
        store = Store(config.db_path)
    return store


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI startup/shutdown hook: open the store, and close it on exit.

    Args:
        app: The FastAPI application (unused; required by the lifespan
            protocol).

    Yields:
        Control back to FastAPI for the app's running lifetime.
    """
    get_store()
    print(f"cmd-overwatch running at http://{config.host}:{config.port}")
    yield
    if store is not None:
        store.close()


app = FastAPI(lifespan=lifespan)


async def broadcast(event_dict: dict[str, Any]) -> None:
    """Send one event to every connected dashboard WebSocket.

    Args:
        event_dict: An already-redacted, JSON-serializable event payload.

    A send failure (client gone, socket closed) on one connection must not
    stop delivery to the others, so each send is isolated in its own
    try/except. The loop iterates ``list(active_connections)``, a snapshot
    already safe to mutate the live list under — deferring the prune to
    after the loop is a clarity/simplicity choice (one pass to send, one to
    clean up), not something required to avoid mutating while iterating.
    """
    failed = []
    for ws in list(active_connections):
        try:
            await ws.send_json(event_dict)
        except Exception:
            failed.append(ws)
    for ws in failed:
        if ws in active_connections:
            active_connections.remove(ws)


@app.post("/event")
async def ingest_event(payload: dict[str, Any]) -> dict[str, str]:
    """POST /event — ingest one Claude Code hook event.

    Called by this repo's own hook scripts (see ``hooks/``), not by any
    external caller; the endpoint itself performs no authentication and
    trusts the shape of ``payload`` only loosely (see Step 1). Persists the
    event (redacted) and immediately fans it out to connected dashboard
    clients.

    Args:
        payload: Arbitrary JSON body from the hook. Recognized keys:
            ``phase`` (``"stop"``, or a tool pre/post phase), ``tool_name``,
            ``tool_input``.

    Returns:
        ``{"status": "ok"}`` unconditionally — this endpoint does not
        validate payload contents strictly enough to have an error path;
        an unrecognized shape degrades to an empty/placeholder event rather
        than a 4xx.
    """
    tool_name = payload.get("tool_name", "")
    # --- Step 1: normalize tool_input ---
    # tool_input arrives from an untrusted hook and may be any JSON type; only a
    # mapping is usable. Anything else (string/list/scalar) is treated as empty
    # so a malformed payload can never crash this unauthenticated endpoint.
    raw_input = payload.get("tool_input", {})
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    event: SessionEvent | TaskEvent | ToolEvent

    # --- Step 2: classify the payload into one of the three event shapes ---
    if payload.get("phase") == "stop":
        event = SessionEvent(session_type="stop")
    elif tool_name in ("TaskCreate", "TaskUpdate"):
        event = TaskEvent(
            task_id=tool_input.get("task_id", "unknown"),
            title=tool_input.get("title", "Unknown task"),
            status=tool_input.get("status", "pending"),
        )
    else:
        # Build input_summary from first key:value pair, truncated to 100 chars
        if tool_input:
            first_key = next(iter(tool_input))
            first_val = tool_input[first_key]
            summary = f"{first_key}: {first_val}"
            input_summary = summary[:100]
        else:
            input_summary = ""

        event = ToolEvent(
            phase=payload.get("phase", ""),
            tool_name=tool_name,
            input_summary=input_summary,
        )

    # --- Step 3: persist (redacted) and broadcast the same redacted copy ---
    # The store redacts before persisting; broadcast the same redacted copy.
    clean = get_store().add_event(event.model_dump(mode="json"))
    await broadcast(clean)
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WS /ws — live dashboard feed.

    On connect, replays up to ``REPLAY_LIMIT`` recent persisted events (so a
    freshly opened dashboard is not blank), registers the socket for
    :func:`broadcast`, then blocks reading (and discarding) client frames
    purely to detect disconnect — the dashboard is not expected to send
    anything meaningful over this socket.

    Args:
        ws: The incoming WebSocket connection.
    """
    await ws.accept()
    active_connections.append(ws)
    try:
        # Replay persisted history to the newly connected client
        for event_dict in get_store().recent_events(REPLAY_LIMIT):
            await ws.send_json(event_dict)
        # Keep the connection alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)


@app.get("/health")
async def health() -> dict[str, Any]:
    """GET /health — liveness probe.

    Returns:
        Process status, current WebSocket connection count, and total
        stored event count. No error responses.
    """
    return {
        "status": "running",
        "connections": len(active_connections),
        "stored_events": get_store().event_count(),
    }


# ---------------------------------------------------------------------------
# Health board / issues / approvals API (read paths + the two write paths —
# decide() and reexecute() — that can dispatch a solution; see the module
# docstring above for why "the one gated write" undersells reexecute()).
# ---------------------------------------------------------------------------

# "Active" = still on the board; excludes "resolved", "failed", and "wontfix",
# which drop off the health board once decided. Not all three are terminal in
# the lifecycle sense: _TRANSITIONS["failed"] allows a redraft back to
# "drafted" (see overwatch.store), so "failed" can return to the board later —
# this list is about health-board visibility, not the state machine's edges.
_ACTIVE_STATUSES = ("open", "drafted", "pending_approval", "executing")
# Lower number = worse severity: a `<` comparison picks the worst per tile, and
# `sorted(...)` on this order surfaces the worst tiles first in the response.
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@app.get("/api/health-board")
async def health_board() -> dict[str, Any]:
    """GET /api/health-board — dashboard summary tiles.

    Returns:
        ``tiles``: one entry per target (or per issue ``source`` when an
        issue has no target in its evidence), each with its active-issue
        count and worst severity, sorted worst-first.
        ``host_metrics``: latest reading per host-health metric.
        ``active_by_severity``: counts across all active issues.
        ``pending_approvals``: count of issues awaiting an operator
        decision.
        ``dry_run``: the current :attr:`overwatch.config.Config.dry_run`
        value, so the dashboard can show whether approving a solution would
        actually execute anything.

    No error responses — an empty store yields an all-zero board.
    """
    store = get_store()
    issues = store.list_issues()
    active = [i for i in issues if i["status"] in _ACTIVE_STATUSES]

    # --- Step 1: group active issues into per-target tiles ---
    by_target: dict[str, dict[str, Any]] = {}
    for issue in active:
        target = str((issue.get("evidence") or {}).get("target") or issue["source"])
        tile = by_target.setdefault(
            target, {"target": target, "active_issues": 0, "worst_severity": "low"}
        )
        tile["active_issues"] += 1
        if _SEV_ORDER[issue["severity"]] < _SEV_ORDER[tile["worst_severity"]]:
            tile["worst_severity"] = issue["severity"]

    # --- Step 2: tally active issues by severity across all targets ---
    severities: dict[str, int] = {}
    for issue in active:
        severities[issue["severity"]] = severities.get(issue["severity"], 0) + 1

    # --- Step 3: assemble the response, worst-severity tiles first ---
    return {
        "tiles": sorted(by_target.values(), key=lambda t: _SEV_ORDER[t["worst_severity"]]),
        "host_metrics": store.latest_host_health(),
        "active_by_severity": severities,
        "pending_approvals": len(store.list_issues(status="pending_approval")),
        "dry_run": config.dry_run,
    }


@app.get("/api/issues")
async def list_issues(status: str | None = None) -> list[dict[str, Any]]:
    """GET /api/issues — list issues, optionally filtered by status.

    Args:
        status: Optional exact-match status filter (e.g. ``"open"``). Omit
            to list every issue regardless of status.

    Returns:
        Matching issue row dicts, most recently seen first. No error
        responses — an unknown status string simply yields an empty list.
    """
    return get_store().list_issues(status=status)


@app.get("/api/issues/{issue_id}")
async def issue_detail(issue_id: int) -> dict[str, Any]:
    """GET /api/issues/{issue_id} — one issue with all its drafted solutions.

    Args:
        issue_id: The issue's primary key.

    Returns:
        ``{"issue": ..., "solutions": [...]}`` where each solution dict also
        carries its recorded ``approval`` (``None`` if undecided).

    Raises:
        HTTPException: 404 if no issue with this id exists.
    """
    store = get_store()
    issue = store.get_issue(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    solutions = store.solutions_for_issue(issue_id)
    for sol in solutions:
        sol["approval"] = store.approval_for_solution(sol["id"])
    return {"issue": issue, "solutions": solutions}


@app.get("/api/approvals/pending")
async def pending_approvals() -> list[dict[str, Any]]:
    """GET /api/approvals/pending — the operator's approval queue.

    Returns:
        One ``{"issue": ..., "solution": ...}`` entry per issue currently
        ``pending_approval`` that still has an undecided solution. Only the
        most recently drafted undecided solution is surfaced per issue
        (``undecided[-1]``) — an issue can accumulate more than one solution
        over its lifecycle (e.g. after a failed attempt is redrafted), and
        only the latest is actionable. No error responses.
    """
    store = get_store()
    out: list[dict[str, Any]] = []
    for issue in store.list_issues(status="pending_approval"):
        undecided = [
            s
            for s in store.solutions_for_issue(issue["id"])
            if store.approval_for_solution(s["id"]) is None
        ]
        if undecided:
            out.append({"issue": issue, "solution": undecided[-1]})
    return out


class Decision(BaseModel):
    """Request body for POST /api/approvals/{solution_id}/decision.

    Attributes:
        decision: ``"approved"`` or ``"denied"``.
        wontfix: Only meaningful with ``"denied"`` — when true, closes the
            issue permanently (``wontfix``, never reopens on recurrence)
            instead of returning it to ``"open"``.
    """

    decision: str  # "approved" | "denied"
    wontfix: bool = False  # with "denied": close permanently instead of reopening


@app.post("/api/approvals/{solution_id}/decision")
async def decide(solution_id: int, body: Decision) -> dict[str, Any]:
    """POST /api/approvals/{solution_id}/decision — the gated write path.

    One of TWO endpoints that can trigger live action, not the only one:
    ``POST /api/solutions/{id}/execute`` (``reexecute`` below) also dispatches,
    without this endpoint's decision check. An "approved" decision on a gated
    solution dispatches it for execution (subject to the executor's own re-check
    of authorization and ``dry_run``).

    Args:
        solution_id: The solution being decided.
        body: The operator's decision.

    Returns:
        For ``"denied"``: the resulting issue status (``"wontfix"`` or
        ``"open"``). For ``"approved"``: the dispatch outcome — status,
        detail, branch, and transcript path (``None`` for a report-only or
        not-yet-implemented runner).

    Raises:
        HTTPException: 404 if the solution or its issue does not exist;
            409 if the issue is not currently ``pending_approval``, or if
            this solution already has a recorded decision (a decision may
            only be recorded once per solution — see
            :meth:`overwatch.store.Store.record_approval`).
    """
    # --- Step 1: validate the solution/issue exist and are awaiting a decision ---
    store = get_store()
    solution = store.get_solution(solution_id)
    if solution is None:
        raise HTTPException(status_code=404, detail="solution not found")
    issue = store.get_issue(solution["issue_id"])
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if issue["status"] != "pending_approval":
        raise HTTPException(
            status_code=409, detail=f"issue is {issue['status']!r}, not pending approval"
        )

    # --- Step 2: record the decision (append-only; rejects a second decision) ---
    try:
        store.record_approval(solution_id, issue["id"], body.decision, DECIDED_BY)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # --- Step 3: denied reopens (or closes permanently); approved dispatches ---
    if body.decision == "denied":
        store.set_issue_status(issue["id"], "wontfix" if body.wontfix else "open")
        return {"status": "denied", "issue_status": "wontfix" if body.wontfix else "open"}

    # Approved: dispatch. Dry-run returns instantly with the recorded plan;
    # live execution is pushed off the event loop so a long-running
    # subprocess (see overwatch.solution.executor) never blocks other
    # requests or the WebSocket broadcast loop.
    result = await asyncio.to_thread(dispatch_solution, store, config, solution)
    return {
        "status": "approved",
        "execution": {
            "status": result.status,
            "detail": result.detail,
            "branch": result.branch,
            "transcript": str(result.transcript_path) if result.transcript_path else None,
        },
    }


@app.post("/api/solutions/{solution_id}/execute")
async def reexecute(solution_id: int) -> dict[str, Any]:
    """POST /api/solutions/{solution_id}/execute — re-dispatch a solution.

    For example, retrying after flipping ``dry_run`` off, or after a
    transient failure (e.g. the ``claude`` CLI was temporarily unavailable).
    The executor re-checks authorization itself; unapproved gated work is
    refused rather than executed just because this endpoint was called.

    Args:
        solution_id: The solution to re-dispatch.

    Returns:
        The dispatch outcome (status, detail, branch) — no transcript path,
        unlike ``decide()``'s response.

    Raises:
        HTTPException: 404 if the solution does not exist.
    """
    store = get_store()
    solution = store.get_solution(solution_id)
    if solution is None:
        raise HTTPException(status_code=404, detail="solution not found")
    result = await asyncio.to_thread(dispatch_solution, store, config, solution)
    return {"status": result.status, "detail": result.detail, "branch": result.branch}


def _read_report(stored_path: str) -> str | None:
    """Read an AAR file, but only if it stays within the configured reports dir.

    The stored path is normally written by the generator under ``reports_dir``;
    this containment check ensures a tampered/foreign record cannot turn this
    endpoint into an arbitrary file read.

    Args:
        stored_path: The path recorded on the AAR record.

    Returns:
        File contents, or ``None`` if the path escapes ``reports_dir``, does
        not exist, or cannot be read (I/O error).
    """
    try:
        resolved = Path(stored_path).resolve()
        if not resolved.is_relative_to(config.reports_dir.resolve()):
            return None
        return resolved.read_text(encoding="utf-8") if resolved.exists() else None
    except OSError:
        return None


@app.get("/api/aar/latest")
async def latest_aar() -> dict[str, Any]:
    """GET /api/aar/latest — most recent after-action report.

    Returns:
        The latest AAR record with its file ``content`` attached (``None``
        if the file is missing or fails the containment check in
        :func:`_read_report`).

    Raises:
        HTTPException: 404 if no AAR has ever been recorded.
    """
    record = get_store().latest_aar()
    if record is None:
        raise HTTPException(status_code=404, detail="no AAR generated yet")
    result = dict(record)
    result["content"] = _read_report(record["path"])
    return result


# Static file mount MUST be last: FastAPI/Starlette matches routes in
# registration order, and StaticFiles(html=True) mounted at "/" would
# otherwise shadow every API route declared after it.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
