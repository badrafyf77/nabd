"""The async ``model <-> tools`` loop shared by the NABD agents.

Limits live in code: each agent invocation may make ``agent_tool_calls`` tool
calls, and all agents of a turn share ``RunContext.budget``. Once either is
used up, the model is called with tools switched off and must answer in words.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage, message_chunk_to_message
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from nabd.runtime.events import RunContext

LIMIT_REACHED = {
    "error": "limit_reached",
    "message": "The tool call limit for this answer is reached. Answer now with what you already know.",
}


class LoopState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_made: int


def tools_allowed(state: dict[str, Any], run: RunContext) -> bool:
    return state.get("tool_calls_made", 0) < run.agent_tool_calls and run.budget.used < run.budget.limit


async def invoke_model(
    model: BaseChatModel,
    schemas: list[dict[str, Any]],
    system_prompt: str,
    messages: list[AnyMessage],
    *,
    allow_tools: bool,
    response_format: Any = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
) -> AIMessage:
    kwargs: dict[str, Any] = {"tool_choice": "auto" if allow_tools else "none"}
    if response_format is not None:
        kwargs["response_format"] = response_format
    bound = model.bind_tools(schemas, **kwargs)
    full = None
    async for chunk in bound.astream([SystemMessage(system_prompt), *messages]):
        full = chunk if full is None else full + chunk
        if on_text is not None and chunk.text:
            await on_text(chunk.text)
    if full is None:
        return AIMessage("")
    return message_chunk_to_message(full)


async def run_tool_calls(
    state: dict[str, Any],
    run: RunContext,
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run the last message's tool calls in order, within both limits."""
    made = state.get("tool_calls_made", 0)
    results = []
    for tool_call in state["messages"][-1].tool_calls:
        if made >= run.agent_tool_calls or not run.budget.take():
            result = LIMIT_REACHED
        else:
            made += 1
            result = await execute(tool_call)
        results.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=tool_call["id"],
                                   name=tool_call["name"]))
    return {"messages": results, "tool_calls_made": made}


def add_loop(graph: StateGraph, model_node, tools_node) -> None:
    graph.add_node("model", model_node)
    graph.add_node("tools", tools_node)
    graph.add_conditional_edges(
        "model", lambda state: "tools" if getattr(state["messages"][-1], "tool_calls", None) else END,
        ["tools", END])
    graph.add_edge("tools", "model")
