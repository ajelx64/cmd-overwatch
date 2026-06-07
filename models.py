from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ToolEvent(BaseModel):
    event_type: Literal["tool"] = "tool"
    phase: str  # "pre" or "post"
    tool_name: str
    input_summary: str = ""
    duration_ms: float | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class TaskEvent(BaseModel):
    event_type: Literal["task"] = "task"
    task_id: str
    title: str
    status: str  # "pending", "in_progress", "completed", "deleted"
    last_tool: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class SessionEvent(BaseModel):
    event_type: Literal["session"] = "session"
    session_type: str  # "stop", "start"
    timestamp: datetime = Field(default_factory=_utcnow)


AnyEvent = ToolEvent | TaskEvent | SessionEvent
