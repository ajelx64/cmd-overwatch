import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import SessionEvent, TaskEvent, ToolEvent
from overwatch.config import Config, load_config
from overwatch.solution.pipeline import dispatch_solution
from overwatch.store import Store

DECIDED_BY = "localhost-operator"  # click-as-operator: valid only on 127.0.0.1

config: Config = load_config()
store: Store | None = None
active_connections: list[WebSocket] = []

REPLAY_LIMIT = 500


def get_store() -> Store:
    """Lazily open the shared store (lets tests point OVERWATCH_CONFIG at a tmp dir)."""
    global store
    if store is None:
        store = Store(config.db_path)
    return store


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_store()
    print(f"cmd-overwatch running at http://{config.host}:{config.port}")
    yield
    if store is not None:
        store.close()


app = FastAPI(lifespan=lifespan)


async def broadcast(event_dict: dict[str, Any]) -> None:
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
    tool_name = payload.get("tool_name", "")
    event: SessionEvent | TaskEvent | ToolEvent

    if payload.get("phase") == "stop":
        event = SessionEvent(session_type="stop")
    elif tool_name in ("TaskCreate", "TaskUpdate"):
        tool_input = payload.get("tool_input", {})
        event = TaskEvent(
            task_id=tool_input.get("task_id", "unknown"),
            title=tool_input.get("title", "Unknown task"),
            status=tool_input.get("status", "pending"),
        )
    else:
        tool_input = payload.get("tool_input", {}) or {}
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

    # The store redacts before persisting; broadcast the same redacted copy.
    clean = get_store().add_event(event.model_dump(mode="json"))
    await broadcast(clean)
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
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
    return {
        "status": "running",
        "connections": len(active_connections),
        "stored_events": get_store().event_count(),
    }


# ---------------------------------------------------------------------------
# Health board / issues / approvals API (read paths + the one gated write)
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = ("open", "drafted", "pending_approval", "executing")
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@app.get("/api/health-board")
async def health_board() -> dict[str, Any]:
    store = get_store()
    issues = store.list_issues()
    active = [i for i in issues if i["status"] in _ACTIVE_STATUSES]

    by_target: dict[str, dict[str, Any]] = {}
    for issue in active:
        target = str((issue.get("evidence") or {}).get("target") or issue["source"])
        tile = by_target.setdefault(
            target, {"target": target, "active_issues": 0, "worst_severity": "low"}
        )
        tile["active_issues"] += 1
        if _SEV_ORDER[issue["severity"]] < _SEV_ORDER[tile["worst_severity"]]:
            tile["worst_severity"] = issue["severity"]

    severities: dict[str, int] = {}
    for issue in active:
        severities[issue["severity"]] = severities.get(issue["severity"], 0) + 1

    return {
        "tiles": sorted(by_target.values(), key=lambda t: _SEV_ORDER[t["worst_severity"]]),
        "host_metrics": store.latest_host_health(),
        "active_by_severity": severities,
        "pending_approvals": len(store.list_issues(status="pending_approval")),
        "dry_run": config.dry_run,
    }


@app.get("/api/issues")
async def list_issues(status: str | None = None) -> list[dict[str, Any]]:
    return get_store().list_issues(status=status)


@app.get("/api/issues/{issue_id}")
async def issue_detail(issue_id: int) -> dict[str, Any]:
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
    decision: str  # "approved" | "denied"
    wontfix: bool = False  # with "denied": close permanently instead of reopening


@app.post("/api/approvals/{solution_id}/decision")
async def decide(solution_id: int, body: Decision) -> dict[str, Any]:
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

    try:
        store.record_approval(solution_id, issue["id"], body.decision, DECIDED_BY)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if body.decision == "denied":
        store.set_issue_status(issue["id"], "wontfix" if body.wontfix else "open")
        return {"status": "denied", "issue_status": "wontfix" if body.wontfix else "open"}

    # Approved: dispatch. Dry-run returns instantly with the recorded plan;
    # live execution is pushed off the event loop.
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
    """Re-dispatch an already-approved solution (e.g. after flipping dry_run off).

    The executor re-checks authorization itself; unapproved gated work is refused.
    """
    store = get_store()
    solution = store.get_solution(solution_id)
    if solution is None:
        raise HTTPException(status_code=404, detail="solution not found")
    result = await asyncio.to_thread(dispatch_solution, store, config, solution)
    return {"status": result.status, "detail": result.detail, "branch": result.branch}


@app.get("/api/aar/latest")
async def latest_aar() -> dict[str, Any]:
    record = get_store().latest_aar()
    if record is None:
        raise HTTPException(status_code=404, detail="no AAR generated yet")
    return record


# Static file mount MUST be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")
