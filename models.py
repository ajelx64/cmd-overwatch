from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ToolEvent(BaseModel):
    event_type: Literal["tool"] = "tool"
    phase: str  # "pre" or "post"
    tool_name: str
    input_summary: str = ""
    duration_ms: float | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class TaskEvent(BaseModel):
    event_type: Literal["task"] = "task"
    task_id: str
    title: str
    status: str  # "pending", "in_progress", "completed", "deleted"
    last_tool: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SessionEvent(BaseModel):
    event_type: Literal["session"] = "session"
    session_type: str  # "stop", "start"
    timestamp: datetime = Field(default_factory=datetime.utcnow)


AnyEvent = ToolEvent | TaskEvent | SessionEvent
