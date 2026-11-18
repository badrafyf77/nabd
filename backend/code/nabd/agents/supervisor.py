"""The Supervisor: the only agent that writes to the customer.

A LangGraph ``model <-> tools`` graph checkpointed per conversation in
PostgreSQL. Each run adds the customer's message (with its stored message ID,
so a retried run cannot add it twice) and streams the reply text into
coalesced events. ``ask_orders_specialist`` awaits the Orders specialist
inside the turn while the Supervisor's activity shows it is waiting on it.

A retried run continues where the interrupted attempt stopped: it resumes the
checkpoint if this run left pending graph work, reuses the finished answer if
the graph had already completed, and otherwise starts the turn.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import START, StateGraph

from nabd.agents.loop import LoopState, add_loop, invoke_model, run_tool_calls, tools_allowed
from nabd.agents.mcp_tools import McpTools, tool_activity
from nabd.agents.orders import OrdersSpecialist
from nabd.runtime.events import Activity, DeltaBuffer, RunContext

PROMPT = Path(__file__).with_name("prompts") / "supervisor.md"
TOOLS = ("get_knowledge", "list_cases")
ASK_ORDERS = {
    "type": "function",
    "function": {
        "name": "ask_orders_specialist",
        "description": (
            "Ask the Orders specialist about this customer's orders, deliveries or returns. It checks "
            "Salesforce and returns findings (outcome, summary, verified facts, questions). Include every "
            "detail the customer gave; it cannot see the chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "The complete question, e.g. "
                                                                          "'Where is order SE-1042?'"}},
            "required": ["request"],
            "additionalProperties": False,
        },
    },
}


class SupervisorState(LoopState, total=False):
    run_id: str  # the run that last started a turn on this conversation
    completed_run: str  # set when that run's turn reached a final answer


@dataclass(frozen=True)
class Turn:
    message_id: str
    text: str
    customer_name: str
    today: date
    knowledge_index: list[dict[str, Any]]


def human_date(value: date) -> str:
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B %Y')} ({value.isoformat()})"


def _index_lines(entries: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {entry['key']}: {entry.get('question') or entry['title']}" for entry in entries) or "- none"


def reply_text(messages: list[Any], message_id: str) -> str:
    """The assistant text written after this run's customer message."""
    start = next((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.id == message_id), None)
    if start is None:
        return ""
    parts = [m.text.strip() for m in messages[start + 1:] if isinstance(m, AIMessage) and m.text.strip()]
    return "\n\n".join(parts)


def dangling_tool_results(messages: list[Any]) -> list[ToolMessage]:
    """Close tool calls an abandoned run left unanswered, so the model history stays valid."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        ToolMessage(content='{"error": "cancelled", "message": "This step was interrupted."}',
                    tool_call_id=call["id"], name=call["name"])
        for m in messages if isinstance(m, AIMessage) for call in m.tool_calls if call["id"] not in answered
    ]


class Supervisor:
    def __init__(self, model: BaseChatModel, orders_model: BaseChatModel, checkpointer: Any):
        self.model = model
        self.orders_model = orders_model
        self.checkpointer = checkpointer

    def system_prompt(self, turn: Turn) -> str:
        return (PROMPT.read_text(encoding="utf-8")
                .replace("{today}", human_date(turn.today))
                .replace("{customer_name}", turn.customer_name)
                .replace("{knowledge_index}", _index_lines(turn.knowledge_index)))

    def graph(self, run: RunContext, tools: McpTools, turn: Turn, deltas: DeltaBuffer, me: Activity):
        schemas = [*tools.schemas(TOOLS), ASK_ORDERS]
        specialist = OrdersSpecialist(self.orders_model, tools, turn.today)
        prompt = self.system_prompt(turn)

        async def model_node(state: dict[str, Any]) -> dict[str, Any]:
            first = True

            async def on_text(piece: str) -> None:
                nonlocal first
                if first and deltas.text:
                    piece = "\n\n" + piece.lstrip()
                first = False
                await deltas.add(piece)

            reply = await invoke_model(self.model, schemas, prompt, state["messages"],
                                       allow_tools=tools_allowed(state, run), on_text=on_text)
            update: dict[str, Any] = {"messages": [reply]}
            if not reply.tool_calls:
                update["completed_run"] = state.get("run_id")
            return update

        async def ask_orders(tool_call: dict[str, Any]) -> dict[str, Any]:
            request = str((tool_call.get("args") or {}).get("request") or "").strip()
            if not request:
                return {"error": "invalid_argument", "message": "Missing argument: request."}
            orders = run.activity(tool_call["id"], "agent.orders", parent=me)
            await orders.start()
            await me.wait("dependency", on=orders.id)
            try:
                findings = await specialist.investigate(run, request, orders)
            except Exception:
                await orders.fail("failed")
                raise
            await orders.complete(**findings.public())
            await me.resume()
            return findings.for_supervisor()

        async def tools_node(state: dict[str, Any]) -> dict[str, Any]:
            async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
                if tool_call["name"] == ASK_ORDERS["function"]["name"]:
                    return await ask_orders(tool_call)
                outcome = await tools.call(run, tool_call["name"], tool_call.get("args") or {}, allowed=TOOLS,
                                           activity=tool_activity(run, tool_call, me))
                return outcome.for_model()

            return await run_tool_calls(state, run, execute)

        graph = StateGraph(SupervisorState)
        add_loop(graph, model_node, tools_node)
        graph.add_edge(START, "model")
        return graph.compile(checkpointer=self.checkpointer, name="nabd_supervisor")

    async def run_turn(self, run: RunContext, tools: McpTools, turn: Turn,
                       on_delta: Callable[[DeltaBuffer], None] | None = None) -> str:
        me = await run.activity("supervisor", "agent.supervisor").start()
        deltas = DeltaBuffer(run, assistant_message_id(run.run_id))
        if on_delta:
            on_delta(deltas)
        graph = self.graph(run, tools, turn, deltas, me)
        config = {"configurable": {"thread_id": run.conversation_id}, "run_name": "nabd_supervisor",
                  "recursion_limit": 2 * run.agent_tool_calls + 6,
                  "metadata": {"agent": "supervisor", "run_id": run.run_id}}
        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        if values.get("completed_run") == run.run_id:
            pass  # the previous attempt finished the graph; only the bookkeeping was lost
        elif snapshot.next and values.get("run_id") == run.run_id:
            await graph.ainvoke(None, config)
        else:
            if repairs := dangling_tool_results(values.get("messages", [])):
                await graph.aupdate_state(config, {"messages": repairs}, as_node="tools")
            await graph.ainvoke({"messages": [HumanMessage(turn.text, id=turn.message_id)],
                                 "run_id": run.run_id, "tool_calls_made": 0}, config)
        await deltas.flush()
        final = await graph.aget_state(config)
        text = reply_text(final.values.get("messages", []), turn.message_id)
        await me.complete()
        return text


def assistant_message_id(run_id: str) -> str:
    """The reply's stable ID, the same for every attempt of a run."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nabd:assistant:{run_id}"))
