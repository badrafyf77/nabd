"""The Complaint specialist: finds the distinct complaints in a customer message.

It never talks to the customer and never writes anything. It reads the
conversation, the customer's case history (fetched by code before it runs)
and, when it needs them, the customer's orders; then it returns a typed
assessment: no complaint, a clarification question, or one or more issues
with a proposed case action.

``verify_assessment`` keeps only what the specialist could support: case
numbers from the candidate list, customer message IDs from this conversation,
order numbers seen in tool results, and facts whose evidence references were
returned by tools in this turn. Everything else becomes a reported claim or is
dropped. The validated case workflow (``nabd.agents.cases``) decides what to
do with the result.
"""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from nabd.agents.language import MessageLanguage, TurnLanguage, original_script
from nabd.agents.loop import LoopState, add_loop, invoke_model, run_tool_calls, tools_allowed
from nabd.agents.mcp_tools import McpTools, tool_activity
from nabd.runtime.events import Activity, RunContext

PROMPT = Path(__file__).with_name("prompts") / "complaints.md"
TOOLS = ("list_orders", "get_order")

Category = Literal["Delivery", "Orders_Products", "Billing_Payments", "Returns_Refunds", "General_Support"]
Signal = Literal["safety_incident", "security_incident", "unauthorized_charge", "duplicate_charge", "lost_parcel",
                 "repeated_unresolved"]
StaffAction = Literal["none", "refund", "replacement", "billing_review", "carrier_investigation", "other"]
Action = Literal["create", "update", "reopen", "request_closure", "close", "gather_information", "no_action"]


class FactIn(BaseModel):
    statement: str
    evidence: list[str] = Field(description="Evidence references copied from tool results.")


class IssueIn(BaseModel):
    """One distinct problem. The model fills every field; code checks them."""

    title: str = Field(description="Short English case subject, such as 'Cast iron pot SE-1061 delayed'.")
    existing_case: str | None = Field(description="Case number from the case history when this message is about "
                                                  "that same problem; null otherwise.")
    match: Literal["new", "same", "unsure"] = Field(
        description="new: no case in the history is about this problem. same: existing_case is about this same "
                    "problem. unsure: it might be; ask the customer.")
    action: Action
    category: Category
    category_confidence: float = Field(description="0 to 1: how sure you are of the category.")
    sentiment: Literal["positive", "neutral", "negative", "very_negative"]
    urgency: Literal["low", "medium", "high"]
    urgency_reason: str
    priority_signals: list[Signal] = Field(description="Only signals the customer reported or tools showed.")
    staff_action: StaffAction = Field(description="Work only staff can do, such as a refund; none otherwise.")
    language: Literal["en", "fr", "ar", "ary", "mixed"] = Field(description="Language of the customer's words.")
    order_numbers: list[str]
    customer_message_ids: list[str] = Field(description="IDs of the customer messages describing this issue.")
    reported: list[str] = Field(description="What the customer says that no tool result confirms, in English.")
    verified_facts: list[FactIn]
    english_summary: str = Field(description="Two to four plain English sentences for staff.")
    confirmation_message_id: str | None = Field(
        description="For close: the ID of the customer message confirming the problem is solved.")
    question: str | None = Field(description="For gather_information or unsure: the one question to ask.")


class ComplaintAssessment(BaseModel):
    """Structured output: the specialist must answer in exactly this shape."""

    outcome: Literal["no_complaint", "needs_clarification", "issues"]
    question: str | None = Field(description="For needs_clarification: the one question to ask the customer.")
    issues: list[IssueIn]
    language: TurnLanguage | None = Field(default=None, description="Select the response language/script on EVERY "
                                         "turn, including greetings and non-complaints, using the current message "
                                         "and explicit preferences; use history for short/mixed replies.")
    message_languages: list[MessageLanguage] = Field(default_factory=list, description="Language of each cited "
                                                    "customer evidence message, using its exact message ID.")


@dataclass(frozen=True)
class ChatLine:
    """One stored conversation message, as the specialist sees it."""

    role: Literal["customer", "assistant", "staff"]
    message_id: str
    text: str
    at: datetime | None = None


@dataclass(frozen=True)
class Candidate:
    """One of the customer's cases, as read from Salesforce for this turn."""

    case_number: str
    case_id: str
    version: str  # SystemModstamp as the gateway expects it
    status: str
    is_closed: bool
    staff_action: str | None
    staff_action_completed: bool
    order_number: str | None
    subject: str | None
    reopened_times: int = 0

    @property
    def staff_action_pending(self) -> bool:
        return bool(self.staff_action) and not self.staff_action_completed


@dataclass
class Fact:
    statement: str
    evidence: list[str]


@dataclass
class Issue:
    ref: str
    title: str
    match: str
    action: str
    existing_case: str | None
    category: str
    category_confidence: float
    sentiment: str
    urgency: str
    urgency_reason: str
    signals: list[str]
    staff_action: str | None
    language: str
    order_numbers: list[str]
    message_ids: list[str]
    reported: list[str]
    facts: list[Fact]
    summary: str
    confirmation_message_id: str | None
    question: str | None

    def to_json(self) -> dict[str, Any]:
        return {**self.__dict__, "facts": [fact.__dict__ for fact in self.facts]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Issue:
        return cls(**{**data, "facts": [Fact(**fact) for fact in data.get("facts", [])]})


@dataclass
class Assessment:
    outcome: str
    question: str | None = None
    issues: list[Issue] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    tool_calls: int = 0
    language: dict[str, str] | None = None
    message_languages: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "question": self.question, "unverified": self.unverified,
                "tool_calls": self.tool_calls, "issues": [issue.to_json() for issue in self.issues],
                "language": self.language, "message_languages": self.message_languages}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Assessment:
        return cls(outcome=data["outcome"], question=data.get("question"), unverified=data.get("unverified", []),
                   tool_calls=data.get("tool_calls", 0),
                   language=data.get("language"), message_languages=data.get("message_languages", {}),
                   issues=[Issue.from_json(issue) for issue in data.get("issues", [])])

    def public(self) -> dict[str, Any]:
        """What the customer's activity card may show: the kind of outcome and how many issues."""
        return {"outcome": self.outcome, "issues": len(self.issues)}


def verify_assessment(raw: str, *, evidence: set[str], orders_seen: set[str], candidates: dict[str, Candidate],
                      customer_messages: dict[str, ChatLine], current_message_id: str,
                      tool_calls: int = 0) -> Assessment:
    """Keep only supported parts of the specialist's answer (see the module docstring)."""
    try:
        parsed = ComplaintAssessment.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        # An unreadable answer must not silently drop a complaint: ask the customer instead.
        return Assessment("needs_clarification", "Could you tell me a little more about the problem?",
                          tool_calls=tool_calls)
    known_orders = orders_seen | {c.order_number for c in candidates.values() if c.order_number}
    issues, unverified = [], []
    for index, item in enumerate(parsed.issues if parsed.outcome == "issues" else [], 1):
        facts = []
        for fact in item.verified_facts:
            if fact.evidence and set(fact.evidence) <= evidence:
                facts.append(Fact(fact.statement.strip(), list(dict.fromkeys(fact.evidence))))
            else:
                unverified.append(fact.statement.strip())
        match, existing = item.match, item.existing_case
        if existing not in candidates:
            existing = None
        if match == "same" and existing is None:
            match = "unsure"  # never act on a case number the tools did not return
        if match == "new":
            existing = None
        message_ids = [m for m in dict.fromkeys(item.customer_message_ids) if m in customer_messages]
        if not message_ids:
            message_ids = [current_message_id]
        confirmation = item.confirmation_message_id if item.confirmation_message_id in customer_messages else None
        issues.append(Issue(
            ref=f"issue-{index}", title=item.title.strip()[:255] or "Customer complaint", match=match,
            action=item.action, existing_case=existing, category=item.category,
            category_confidence=min(1.0, max(0.0, float(item.category_confidence))), sentiment=item.sentiment,
            urgency=item.urgency, urgency_reason=item.urgency_reason.strip(),
            signals=list(dict.fromkeys(item.priority_signals)),
            staff_action=None if item.staff_action == "none" else item.staff_action, language=item.language,
            order_numbers=[n for n in dict.fromkeys(o.strip().upper() for o in item.order_numbers) if n in known_orders],
            message_ids=message_ids, reported=[r.strip() for r in item.reported if r.strip()], facts=facts,
            summary=item.english_summary.strip() or item.title.strip(), confirmation_message_id=confirmation,
            question=(item.question or "").strip() or None,
        ))
    outcome = parsed.outcome
    if outcome == "issues" and not issues:
        outcome = "no_complaint"
    question = None
    if outcome == "needs_clarification":
        question = (parsed.question or "").strip() or "Could you tell me a little more about the problem?"
    labels = {label.message_id: label.language for label in parsed.message_languages
              if label.message_id in customer_messages}
    current = customer_messages.get(current_message_id)
    language = parsed.language.public(original_script(current.text) if current else None) if parsed.language else None
    if language:
        labels[current_message_id] = language["input_language"]
    return Assessment(outcome, question, issues, unverified, tool_calls, language, labels)


class ComplaintState(LoopState, total=False):
    evidence: Annotated[list[str], operator.add]
    orders_seen: Annotated[list[str], operator.add]
    lost_orders: Annotated[list[str], operator.add]


def _orders_in(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(order numbers, lost order numbers) in a list_orders/get_order result."""
    if data.get("found") is not None:
        orders = [data] if data.get("found") and data.get("order_number") else []
    else:
        orders = list(data.get("orders", []))
    numbers = [order["order_number"] for order in orders if order.get("order_number")]
    lost = [order["order_number"] for order in orders
            if order.get("order_number") and (order.get("delivery") or {}).get("status") == "lost"]
    return numbers, lost


def human_date(value: date) -> str:
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B %Y')} ({value.isoformat()})"


@dataclass
class Investigation:
    raw: str
    evidence: set[str]
    orders_seen: set[str]
    lost_orders: set[str]
    tool_calls: int


class ComplaintSpecialist:
    def __init__(self, model: BaseChatModel, tools: McpTools, today: date):
        self.model = model
        self.tools = tools
        self.today = today

    def system_prompt(self) -> str:
        return PROMPT.read_text(encoding="utf-8").replace("{today}", human_date(self.today))

    @staticmethod
    def request(history: list[ChatLine], current: ChatLine, case_history: dict[str, Any],
                order_overview: dict[str, Any] | None = None) -> str:
        lines = ["Conversation so far (oldest first):"]
        earlier = [line for line in history if line.message_id != current.message_id]
        for line in earlier or []:
            label = f"customer {line.message_id}" if line.role == "customer" else line.role
            lines.append(f"[{label}] {line.text}")
        if not earlier:
            lines.append("(no earlier messages)")
        lines += ["", f"Current customer message: [customer {current.message_id}] {current.text}", "",
                  "The customer's cases from Salesforce (list_case_history):",
                  json.dumps(case_history, ensure_ascii=False)]
        if order_overview is not None:
            lines += ["", "The customer's authorized order overview (list_orders):",
                      json.dumps(order_overview, ensure_ascii=False)]
        return "\n".join(lines)

    def _graph(self, run: RunContext, activity: Activity):
        schemas = self.tools.schemas(TOOLS)

        async def model_node(state: dict[str, Any]) -> dict[str, Any]:
            reply = await invoke_model(self.model, schemas, self.system_prompt(), state["messages"],
                                       allow_tools=tools_allowed(state, run), response_format=ComplaintAssessment)
            return {"messages": [reply]}

        async def tools_node(state: dict[str, Any]) -> dict[str, Any]:
            evidence: list[str] = []
            seen: list[str] = []
            lost: list[str] = []

            async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
                outcome = await self.tools.call(run, tool_call["name"], tool_call.get("args") or {},
                                                allowed=TOOLS, activity=tool_activity(run, tool_call, activity))
                evidence.extend(outcome.evidence)
                if outcome.ok:
                    numbers, lost_numbers = _orders_in(outcome.data)
                    seen.extend(numbers)
                    lost.extend(lost_numbers)
                return outcome.for_model()

            update = await run_tool_calls(state, run, execute)
            return {**update, "evidence": evidence, "orders_seen": seen, "lost_orders": lost}

        graph = StateGraph(ComplaintState)
        add_loop(graph, model_node, tools_node)
        graph.add_edge(START, "model")
        return graph.compile(name="nabd_complaints")

    async def investigate(self, run: RunContext, request: str, activity: Activity) -> Investigation:
        graph = self._graph(run, activity)
        final = await graph.ainvoke(
            {"messages": [HumanMessage(request)], "tool_calls_made": 0, "evidence": [], "orders_seen": [],
             "lost_orders": []},
            {"recursion_limit": 2 * run.agent_tool_calls + 5, "run_name": "nabd_complaints",
             "metadata": {"agent": "complaints", "run_id": run.run_id}},
        )
        return Investigation(final["messages"][-1].text, set(final.get("evidence", [])),
                             set(final.get("orders_seen", [])), set(final.get("lost_orders", [])),
                             final.get("tool_calls_made", 0))
