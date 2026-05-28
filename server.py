from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from models import ToolEvent, TaskEvent, SessionEvent

app = FastAPI()

event_buffer: deque = deque(maxlen=500)
active_connections: list[WebSocket] = []


async def broadcast(event_dict: dict):
    failed = []
    for ws in list(active_connections):
        try:
            await ws.send_json(event_dict)
        except Exception:
            failed.append(ws)
    for ws in failed:
        if ws in active_connections:
            active_connections.remove(ws)


@app.on_event("startup")
async def startup():
    print("Claude Overwatch running at http://localhost:8765")


@app.post("/event")
async def ingest_event(payload: dict):
    tool_name = payload.get("tool_name", "")

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

    event_dict = event.model_dump(mode="json")
    event_buffer.append(event_dict)
    await broadcast(event_dict)
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.append(ws)
    try:
        # Replay buffered history to the newly connected client
        for event_dict in list(event_buffer):
            await ws.send_json(event_dict)
        # Keep the connection alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)


@app.get("/health")
async def health():
    return {
        "status": "running",
        "connections": len(active_connections),
        "buffered_events": len(event_buffer),
    }


# Static file mount MUST be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")
