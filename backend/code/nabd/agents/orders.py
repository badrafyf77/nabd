"""The Orders specialist: read-only investigation of the customer's orders.

It never talks to the customer. It returns typed findings; code then keeps
only the facts whose evidence references were really returned by its own
tool calls in this investigation, and reports the rest as unverified.
"""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from nabd.agents.loop import LoopState, add_loop, invoke_model, run_tool_calls, tools_allowed
from nabd.agents.mcp_tools import McpTools, tool_activity
from nabd.runtime.events import Activity, RunContext

PROMPT = Path(__file__).with_name("prompts") / "orders.md"
TOOLS = ("list_orders", "get_order")


class Fact(BaseModel):
    statement: str
    evidence: list[str] = Field(description="Evidence references copied from tool results.")


class OrdersFindings(BaseModel):
    """Structured output: the specialist must answer in exactly this shape."""

    outcome: Literal["done", "needs_information", "cannot_complete"]
    summary: str = Field(description="What the Supervisor should know, in one to three plain sentences.")
    facts: list[Fact]
    order_numbers: list[str]
    questions: list[str] = Field(description="Questions the customer must answer; empty unless needs_information.")


@dataclass
class Findings:
    outcome: str
    summary: str
    facts: list[Fact] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    order_numbers: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    tool_calls: int = 0

    def for_supervisor(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "outcome": self.outcome, "summary": self.summary,
            "verified_facts": [fact.statement for fact in self.facts],
            "order_numbers": self.order_numbers, "questions": self.questions,
        }
        if self.unverified:
            result["unverified_statements"] = self.unverified
            result["note"] = "Unverified statements had no matching evidence. Do not present them as facts."
        return result

    def public(self) -> dict[str, Any]:
        """What the customer's activity card shows: confirmed findings only."""
        return {"outcome": self.outcome, "facts": [fact.statement for fact in self.facts],
                "order_numbers": self.order_numbers, "questions": self.questions}


class OrdersState(LoopState, total=False):
    evidence: Annotated[list[str], operator.add]
    orders_seen: Annotated[list[str], operator.add]


def _orders_in(data: dict[str, Any]) -> list[str]:
    if data.get("found") and data.get("order_number"):
        return [data["order_number"]]
    return [order["order_number"] for order in data.get("orders", []) if order.get("order_number")]


def verify_findings(raw: str, evidence: set[str], orders_seen: set[str], tool_calls: int) -> Findings:
    try:
        parsed = OrdersFindings.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return Findings("cannot_complete", "The Orders specialist gave an answer that could not be read.",
                        tool_calls=tool_calls)
    verified, unverified = [], []
    for fact in parsed.facts:
        if fact.evidence and set(fact.evidence) <= evidence:
            verified.append(fact)
        else:
            unverified.append(fact.statement)
    return Findings(
        outcome=parsed.outcome, summary=parsed.summary.strip(), facts=verified, unverified=unverified,
        order_numbers=[number for number in parsed.order_numbers if number in orders_seen],
        questions=parsed.questions if parsed.outcome == "needs_information" else [],
        tool_calls=tool_calls,
    )


class OrdersSpecialist:
    def __init__(self, model: BaseChatModel, tools: McpTools, today: date):
        self.model = model
        self.tools = tools
        self.today = today

    def system_prompt(self) -> str:
        return PROMPT.read_text(encoding="utf-8").replace(
            "{today}", f"{self.today.strftime('%A')} {self.today.day} {self.today.strftime('%B %Y')} "
                       f"({self.today.isoformat()})")

    def _graph(self, run: RunContext, activity: Activity):
        schemas = self.tools.schemas(TOOLS)

        async def model_node(state: dict[str, Any]) -> dict[str, Any]:
            reply = await invoke_model(self.model, schemas, self.system_prompt(), state["messages"],
                                       allow_tools=tools_allowed(state, run), response_format=OrdersFindings)
            return {"messages": [reply]}

        async def tools_node(state: dict[str, Any]) -> dict[str, Any]:
            evidence: list[str] = []
            seen: list[str] = []

            async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
                outcome = await self.tools.call(run, tool_call["name"], tool_call.get("args") or {},
                                                allowed=TOOLS, activity=tool_activity(run, tool_call, activity))
                evidence.extend(outcome.evidence)
                if outcome.ok:
                    seen.extend(_orders_in(outcome.data))
                return outcome.for_model()

            update = await run_tool_calls(state, run, execute)
            return {**update, "evidence": evidence, "orders_seen": seen}

        graph = StateGraph(OrdersState)
        add_loop(graph, model_node, tools_node)
        graph.add_edge(START, "model")
        return graph.compile(name="nabd_orders")

    async def investigate(self, run: RunContext, request: str, activity: Activity) -> Findings:
        graph = self._graph(run, activity)
        final = await graph.ainvoke(
            {"messages": [HumanMessage(request)], "tool_calls_made": 0, "evidence": [], "orders_seen": []},
            {"recursion_limit": 2 * run.agent_tool_calls + 5, "run_name": "nabd_orders",
             "metadata": {"agent": "orders", "run_id": run.run_id}},
        )
        return verify_findings(final["messages"][-1].text, set(final.get("evidence", [])),
                               set(final.get("orders_seen", [])), final.get("tool_calls_made", 0))
