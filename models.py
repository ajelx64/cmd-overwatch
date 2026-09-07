"""Pydantic event schemas shared by the Claude Code hooks and the dashboard.

A hook script builds one of these models when a tool call, task update, or
session boundary happens, then hands it to the store/server layer to persist
and render. Defining the shape once here, instead of separately in the hook
and in the code that reads events back, is what keeps producer and consumer
in sync. Depends only on ``pydantic``.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Return the current time in UTC.

    Used as a ``Field(default_factory=...)`` below rather than passing
    ``datetime.now(UTC)`` directly as a field default — a plain default
    expression is evaluated once at class-definition time and would give
    every instance the same frozen timestamp; a factory is called fresh
    per instance.

    Returns:
        The current timezone-aware (UTC) time.
    """
    return datetime.now(UTC)


class ToolEvent(BaseModel):
    """Record of one side (pre- or post-) of a single tool invocation.

    Attributes:
        event_type: Discriminator tag, always ``"tool"``.
        phase: Which side of the call this is, ``"pre"`` or ``"post"``.
        tool_name: Name of the invoked tool.
        input_summary: Short human-readable rendering of the tool's input.
        duration_ms: How long the call took, in milliseconds. ``None`` on
            the "pre" record, since the call hasn't finished yet.
        timestamp: When this record was created; defaults to now (UTC).
    """

    event_type: Literal["tool"] = "tool"
    phase: str  # "pre" or "post"
    tool_name: str
    input_summary: str = ""
    duration_ms: float | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class TaskEvent(BaseModel):
    """Status update for one tracked task/checklist item.

    Attributes:
        event_type: Discriminator tag, always ``"task"``.
        task_id: Identifier for the task, stable across its status updates.
        title: Human-readable task description.
        status: One of "pending", "in_progress", "completed", "deleted".
        last_tool: Name of the most recent tool associated with this task,
            if any.
        timestamp: When this record was created; defaults to now (UTC).
    """

    event_type: Literal["task"] = "task"
    task_id: str
    title: str
    status: str  # "pending", "in_progress", "completed", "deleted"
    last_tool: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class SessionEvent(BaseModel):
    """Marker for a session starting or stopping.

    Attributes:
        event_type: Discriminator tag, always ``"session"``.
        session_type: Which boundary this is, ``"stop"`` or ``"start"``.
        timestamp: When this record was created; defaults to now (UTC).
    """

    event_type: Literal["session"] = "session"
    session_type: str  # "stop", "start"
    timestamp: datetime = Field(default_factory=_utcnow)


# A tagged union keyed on event_type, so code that handles a stream of mixed
# events can branch on that field instead of needing a separate code path
# per event class.
AnyEvent = ToolEvent | TaskEvent | SessionEvent
