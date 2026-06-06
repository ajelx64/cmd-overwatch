import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from models import SessionEvent, TaskEvent, ToolEvent
from overwatch.config import Config, load_config
from overwatch.store import Store

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


# Static file mount MUST be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")
