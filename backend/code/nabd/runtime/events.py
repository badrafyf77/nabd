"""Public execution events and the hooks that write them.

Events are the customer-visible projection of what a run actually did. Only
code writes them: when a tool call starts, retries, finishes or fails, and
when a specialist is awaited. Payloads hold localization codes and allowed
parameters, never prompts, reasoning, credentials or other customers' data.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from nabd.runtime.trace import MAX_ITEMS, bounded

EventType = Literal[
    "staff.reply", "staff.reply.withdrawn", "case.staff_update", "case.removed",
    "conversation.language",
    "message.created", "message.delta", "message.completed",
    "run.queued", "run.started", "run.retrying", "run.completed", "run.failed",
    "activity",
]
ActivityState = Literal["queued", "running", "waiting", "retrying", "completed", "failed"]


class NewEvent(BaseModel):
    type: EventType
    run_id: str | None = None
    activity_id: str | None = None
    parent_activity_id: str | None = None
    state: ActivityState | None = None
    code: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    async def emit(self, conversation_id: str, event: NewEvent) -> int: ...


@dataclass
class TurnBudget:
    """Tool calls shared by every agent of one turn."""

    limit: int
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass
class RunContext:
    """Everything the hooks of one run attempt need. Never sent to a model."""

    conversation_id: str
    run_id: str
    attempt: int
    customer_id: str
    sink: EventSink
    budget: TurnBudget
    agent_tool_calls: int = 5
    _activity_state: dict[str, str] = field(default_factory=dict)

    trace: list[dict[str, Any]] = field(default_factory=list)
    trace_partial: bool = False
    _activity_agents: dict[str, str] = field(default_factory=dict)

    def record(self, agent: str, kind: str, name: str, *, arguments: Any = None,
               result: Any = None, usage: dict[str, int] | None = None, duration_ms: int | None = None) -> None:
        if len(self.trace) >= MAX_ITEMS:
            self.trace_partial = True
            return
        args, output = bounded(arguments), bounded(result)
        self.trace_partial |= any(isinstance(value, dict) and value.get("truncated") is True
                                  for value in (args, output))
        self.trace.append({"agent": agent, "kind": kind, "name": name, "arguments": args,
                           "result": output, "usage": usage, "duration_ms": duration_ms})

    async def emit(self, event: NewEvent) -> int:
        return await self.sink.emit(self.conversation_id, event.model_copy(update={"run_id": self.run_id}))

    def activity(self, local_id: str, code: str, *, parent: Activity | None = None,
                 params: dict[str, Any] | None = None) -> Activity:
        activity_id = f"{self.run_id}:{local_id}"
        if code.startswith("agent."):
            self._activity_agents[activity_id] = code.removeprefix("agent.")
        return Activity(self, activity_id, code, parent.id if parent else None, params or {})


@dataclass
class Activity:
    run: RunContext
    id: str
    code: str
    parent_id: str | None
    params: dict[str, Any]
    started: float = field(default_factory=time.monotonic)

    async def _emit(self, state: ActivityState, **payload: Any) -> None:
        self.run._activity_state[self.id] = state
        await self.run.emit(NewEvent(
            type="activity", activity_id=self.id, parent_activity_id=self.parent_id, state=state,
            code=self.code, payload={"params": self.params, **payload},
        ))

    async def start(self) -> Activity:
        await self._emit("running")
        return self

    async def wait(self, waiting_for: Literal["dependency", "customer_input", "external_operation"],
                   on: str | None = None, reason: str | None = None) -> None:
        extra = {"waiting_reason": reason} if reason else {}
        await self._emit("waiting", waiting_for=waiting_for, waiting_on=on, **extra)

    async def resume(self) -> None:
        await self._emit("running")

    async def retrying(self, error: str, attempt: int) -> None:
        await self._emit("retrying", error=error, retry=attempt)

    async def complete(self, **result: Any) -> None:
        await self._emit("completed", result=result, duration_ms=self.elapsed_ms)

    async def fail(self, error: str) -> None:
        await self._emit("failed", error=error, duration_ms=self.elapsed_ms)

    @property
    def parent_agent(self) -> str:
        return self.run._activity_agents.get(self.parent_id, "unknown")

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class DeltaBuffer:
    """Coalesces streamed reply text into fewer persisted ``message.delta`` events."""

    def __init__(self, run: RunContext, message_id: str, *, max_chars: int = 160, max_seconds: float = 0.25):
        self.run = run
        self.message_id = message_id
        self.max_chars = max_chars
        self.max_seconds = max_seconds
        self.pending = ""
        self.text = ""
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

    async def add(self, piece: str) -> None:
        if not piece:
            return
        self.pending += piece
        self.text += piece
        if len(self.pending) >= self.max_chars or time.monotonic() - self._last_flush >= self.max_seconds:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self.pending:
                return
            chunk, self.pending = self.pending, ""
            self._last_flush = time.monotonic()
            await self.run.emit(NewEvent(type="message.delta", payload={
                "message_id": self.message_id, "attempt": self.run.attempt, "text": chunk}))
