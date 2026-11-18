"""The agents' side of the read MCP server: one authenticated session per run.

``McpTools`` lists the server's tools once, offers each agent only the tools it
is allowed to use (as OpenAI function schemas), and runs calls through real
MCP requests. Every call is wrapped by an activity, so the customer sees it
start, retry and finish; the result is only reported as completed when the
server confirmed it.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from nabd.runtime.events import Activity, RunContext

_ERROR_CODE = re.compile(r"\[([a-z_]+)\]")
RETRYABLE = {"unavailable", "transport"}
RETRY_DELAY_SECONDS = 1.0


@dataclass
class ToolOutcome:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None  # a code such as "unavailable", "not_allowed", "invalid_argument"
    message: str = ""
    evidence: set[str] = field(default_factory=set)

    def for_model(self) -> dict[str, Any]:
        if self.ok:
            return self.data
        return {"error": self.error, "message": self.message}


def evidence_in(value: Any) -> set[str]:
    """Every evidence reference anywhere in a tool result."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence" and isinstance(item, list):
                found.update(ref for ref in item if isinstance(ref, str))
            else:
                found |= evidence_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= evidence_in(item)
    return found


def _summary(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """What the customer's activity card may show about a confirmed result."""
    if name == "list_orders":
        return {"count": data.get("count", 0)}
    if name == "get_order":
        return {"order_number": data.get("order_number"), "found": bool(data.get("found"))}
    if name == "list_cases":
        return {"count": data.get("count", 0)}
    if name == "get_knowledge":
        return {"articles": [{"title": e["title"], "revision": e["revision_key"]}
                             for e in data.get("entries", [])]}
    return {}


def _params(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_order" and isinstance(args.get("order_number"), str):
        return {"order_number": args["order_number"][:20]}
    return {}


class McpTools:
    """An open MCP client session plus the server's tool list."""

    def __init__(self, client: Client, tools: dict[str, dict[str, Any]]):
        self.client = client
        self.tools = tools

    def schemas(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        missing = [name for name in names if name not in self.tools]
        if missing:
            raise RuntimeError(f"read MCP server does not offer {', '.join(missing)}")
        return [self.tools[name] for name in names]

    async def read_json(self, uri: str) -> dict[str, Any]:
        result = await self.client.read_resource(uri)
        return json.loads(result.contents[0].text)

    async def _call_once(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        try:
            result = await self.client.call_tool(name, args)
        except Exception as error:  # transport failure; never leak details to the model
            return ToolOutcome(False, error="transport", message=f"The read service could not be reached "
                                                                 f"({type(error).__name__}).")
        text = result.content[0].text if result.content else ""
        if result.is_error:
            match = _ERROR_CODE.search(text)
            code = match.group(1) if match else "failed"
            message = text[match.end():].strip() if match else "The tool call failed."
            return ToolOutcome(False, error=code, message=message)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            return ToolOutcome(False, error="failed", message="The tool returned an unreadable result.")
        return ToolOutcome(True, data=data, evidence=evidence_in(data))

    async def call(self, run: RunContext, name: str, args: dict[str, Any], *, allowed: tuple[str, ...],
                   activity: Activity) -> ToolOutcome:
        """One tool call of an agent, with allowlist, retry once on outages, and activity events."""
        await activity.start()
        if name not in allowed:
            outcome = ToolOutcome(False, error="not_allowed", message=f"{name} is not available to this agent.")
            await activity.fail(outcome.error)
            return outcome
        outcome = await self._call_once(name, args)
        if not outcome.ok and outcome.error in RETRYABLE:
            await activity.retrying(outcome.error, 1)
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            outcome = await self._call_once(name, args)
        if outcome.ok:
            await activity.complete(**_summary(name, outcome.data))
        else:
            await activity.fail(outcome.error or "failed")
        return outcome


def tool_activity(run: RunContext, tool_call: dict[str, Any], parent: Activity | None) -> Activity:
    name = tool_call["name"]
    return run.activity(tool_call["id"], f"tool.{name}", parent=parent,
                        params=_params(name, tool_call.get("args") or {}))


def openai_schema(tool: Any) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description or "", "parameters": tool.input_schema}}


McpConnector = Callable[[str], AbstractAsyncContextManager[McpTools]]


def http_connector(url: str, timeout_seconds: float,
                   transport: httpx2.AsyncBaseTransport | None = None) -> McpConnector:
    """Open a Streamable HTTP MCP session that sends the run's context token."""

    @asynccontextmanager
    async def connect(bearer: str) -> AsyncIterator[McpTools]:
        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {bearer}"}, transport=transport,
            timeout=httpx2.Timeout(timeout_seconds), follow_redirects=False,
        )
        try:
            async with http:
                async with Client(streamable_http_client(url, http_client=http), cache=None) as client:
                    listed = await client.list_tools()
                    yield McpTools(client, {tool.name: openai_schema(tool) for tool in listed.tools})
        except BaseExceptionGroup as group:
            raise _single(group) from None

    return connect


def _single(error: BaseException) -> BaseException:
    """The MCP client's task group wraps errors; report a lone error as itself."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error
