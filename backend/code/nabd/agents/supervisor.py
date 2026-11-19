"""The Supervisor: the only agent that writes to the customer.

A LangGraph graph checkpointed per conversation in PostgreSQL: the mandatory
complaint stage (``nabd.agents.cases``) runs first, then the ``model <-> tools``
loop writes a reply that may cite only the case work that stage confirmed. Each run adds the customer's message (with its stored message ID,
so a retried run cannot add it twice) and streams the reply text into
coalesced events. ``ask_orders_specialist`` awaits the Orders specialist
inside the turn while the Supervisor's activity shows it is waiting on it.

A retried run continues where the interrupted attempt stopped: it resumes the
checkpoint if this run left pending graph work, reuses the finished answer if
the graph had already completed, and otherwise starts the turn.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import START, StateGraph

from nabd.agents.loop import LoopState, add_loop, invoke_model, run_tool_calls, tools_allowed
from nabd.agents.language import arabic_words, kept_references, to_arabizi, violates_latin_script
from nabd.agents.mcp_tools import McpTools, tool_activity
from nabd.agents.orders import OrdersSpecialist
from nabd.runtime.events import Activity, DeltaBuffer, RunContext

log = logging.getLogger(__name__)

PROMPT = Path(__file__).with_name("prompts") / "supervisor.md"
LATIN_REWRITE = (
    "You fix the writing system of a ShopEasy customer-support reply in Moroccan Darija. The customer "
    "reads Darija in Latin letters (Arabizi, with 3, 7 and 9 for Arabic sounds). The reply contains the "
    "listed words in Arabic letters. Rewrite each of them as Latin-letter Darija (for example ila, mazal, "
    "dyal, 7tta, flous). Keep every other word, and every fact, amount, date and case/order number, "
    "exactly as written. Do not add or remove information. Return only the corrected reply."
)
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
    case_notes: dict[str, Any] | None  # this turn's complaint stage outcome (confirmed case work)


@dataclass(frozen=True)
class Turn:
    message_id: str
    text: str
    customer_name: str
    today: date
    knowledge_index: list[dict[str, Any]]
    history: tuple[Any, ...] = ()  # ChatLine items up to and including this message
    message_at: datetime | None = None


ComplaintStageCall = Callable[[], Awaitable[dict[str, Any]]]

VERBS = {"create_case": "open", "update_case": "update", "reopen_case": "reopen",
         "request_closure": "update", "close_case": "close"}
REASONS = {
    "case_closed": "the case is already closed", "case_open": "the case is still open",
    "staff_action_pending": "staff still have to finish their part", "version_changed": "staff changed it meanwhile",
    "closed_as_duplicate": "staff closed it as a duplicate", "order_not_found": "the order is not on the account",
    "case_not_found": "the case is not on the account",
}


def case_work_text(notes: dict[str, Any] | None) -> str:
    """The confirmed case work of this turn, written for the Supervisor's prompt."""
    if notes is None:
        return "- No case check ran for this message. Do not say any case was opened or changed."
    lines = []
    for case in notes.get("cases", []):
        title, number = case.get("title") or "the problem", case.get("case_number")
        team = f"the {case['team']} team" if case.get("team") else "a team member"
        status = case.get("case_status")
        if case["status"] == "confirmed":
            lines.append({
                "create_case": f"- Opened case {number} with {team}: \"{title}\". Status: {status}.",
                "update_case": f"- Added this message to existing case {number} (\"{title}\"). Status: {status}.",
                "reopen_case": f"- Reopened case {number} (\"{title}\") because the problem came back. "
                               f"Status: {status}.",
                "request_closure": f"- Case {number} (\"{title}\"): the records show it is solved. Ask the "
                                   "customer to confirm it is solved before it can be closed.",
                "close_case": f"- Closed case {number} (\"{title}\") after the customer confirmed it is solved.",
            }[case["operation"]])
        elif case["status"] == "pending":
            lines.append(f"- Still waiting for Salesforce to confirm a case about \"{title}\". Say it is being "
                         "recorded; give no case number.")
        else:
            reason = REASONS.get(case.get("reason") or "", "it could not be saved right now")
            lines.append(f"- Could not {VERBS[case['operation']]} the case about \"{title}\" ({reason}). "
                         "Do not say it was recorded.")
    lines += [f"- Ask the customer: {question}" for question in notes.get("questions", [])]
    lines += [f"- {note}" for note in notes.get("notes", [])]
    lines += [f"- Checked fact: {fact}" for fact in notes.get("facts", [])]
    if not lines:
        lines.append("- No complaint in this message, so no case was needed.")
    return "\n".join(lines)


def human_date(value: date) -> str:
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B %Y')} ({value.isoformat()})"


def _index_lines(entries: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {entry['key']}: {entry.get('question') or entry['title']}" for entry in entries) or "- none"


def reply_text(messages: list[Any], message_id: str) -> str:
    """The assistant text written after this run's customer message."""
    start = next((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.id == message_id), None)
    if start is None:
        return ""
    parts = [m.text.strip() for m in messages[start + 1:]
             if isinstance(m, AIMessage) and not m.tool_calls and m.text.strip()]
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

    def system_prompt(self, turn: Turn, case_notes: dict[str, Any] | None = None) -> str:
        return (PROMPT.read_text(encoding="utf-8")
                .replace("{today}", human_date(turn.today))
                .replace("{customer_name}", turn.customer_name)
                .replace("{case_work}", case_work_text(case_notes))
                .replace("{staff_updates}", json.dumps(
                    [{"text": line.text, "at": line.at.isoformat() if line.at else None}
                     for line in turn.history if line.role == "staff"], ensure_ascii=False))
                .replace("{language}", json.dumps((case_notes or {}).get("language")))
                .replace("{order_options}", json.dumps((case_notes or {}).get("order_options", []), ensure_ascii=False))
                .replace("{knowledge_index}", _index_lines(turn.knowledge_index)))

    async def _latin_darija(self, schemas: list[dict[str, Any]], draft: str, language: dict[str, str],
                            run: RunContext) -> str:
        """Bring a Latin Darija draft that contains Arabic letters into Latin script.

        A writing-system contract, not a semantic evaluator. One focused rewrite
        (same model, tools disabled, only the draft and its exact Arabic-letter
        words, so the conversation's Arabic text cannot prime it again). The
        rewrite is used only if it keeps every number of the draft. Any Arabic
        letters still left are converted to Arabizi in code, so the turn never
        fails on the script alone.
        """
        words = arabic_words(draft)
        rewrite = await invoke_model(self.model, schemas, LATIN_REWRITE, [HumanMessage(
            "Words written in Arabic letters: " + json.dumps(words, ensure_ascii=False) + "\n\nReply:\n" + draft)],
            allow_tools=False, run=run, agent="supervisor")
        text = rewrite.text.strip()
        if rewrite.tool_calls or not text or not kept_references(draft, text):
            text = draft
        if violates_latin_script(text, language):
            log.warning("latin darija fallback conversion (run %s, %d words)", run.run_id, len(arabic_words(text)))
            text = to_arabizi(text)
        return text

    def graph(self, run: RunContext, tools: McpTools, turn: Turn, deltas: DeltaBuffer, me: Activity,
              stage: ComplaintStageCall | None = None):
        schemas = [*tools.schemas(TOOLS), ASK_ORDERS]
        specialist = OrdersSpecialist(self.orders_model, tools, turn.today)

        async def complaints_node(state: dict[str, Any]) -> dict[str, Any]:
            """Mandatory complaint analysis and case work, before the Supervisor writes its reply."""
            return {"case_notes": await stage() if stage is not None else None}

        async def model_node(state: dict[str, Any]) -> dict[str, Any]:
            prompt = self.system_prompt(turn, state.get("case_notes"))
            language = (state.get("case_notes") or {}).get("language")
            latin = bool(language and language.get("reply_language") == "ary"
                         and language.get("reply_script") == "latin")
            first = True

            async def on_text(piece: str) -> None:
                nonlocal first
                if first and deltas.text:
                    piece = "\n\n" + piece.lstrip()
                first = False
                await deltas.add(piece)

            reply = await invoke_model(self.model, schemas, prompt, state["messages"], run=run, agent="supervisor",
                                       allow_tools=tools_allowed(state, run), on_text=None if latin else on_text)
            if not reply.tool_calls and latin:
                if violates_latin_script(reply.text, language):
                    reply = AIMessage(await self._latin_darija(schemas, reply.text, language, run))
                await on_text(reply.text)  # rejected drafts were never streamed or checkpointed
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
                original = json.dumps({"current_customer_message": turn.text,
                                       "recent_customer_messages": [line.text for line in turn.history
                                                                    if line.role == "customer"][-4:]},
                                      ensure_ascii=False)
                findings = await specialist.investigate(run, request + "\nOriginal customer wording: " + original,
                                                         orders)
            except Exception:
                await orders.fail("failed")
                raise
            await orders.complete(**findings.public())
            await me.resume()
            return findings.for_supervisor()

        async def tools_node(state: dict[str, Any]) -> dict[str, Any]:
            async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
                if tool_call["name"] == ASK_ORDERS["function"]["name"]:
                    result = await ask_orders(tool_call)
                    run.record("supervisor", "tool", tool_call["name"], arguments=tool_call.get("args") or {},
                               result=result)
                    return result
                outcome = await tools.call(run, tool_call["name"], tool_call.get("args") or {}, allowed=TOOLS,
                                           activity=tool_activity(run, tool_call, me))
                return outcome.for_model()

            return await run_tool_calls(state, run, execute)

        graph = StateGraph(SupervisorState)
        graph.add_node("complaints", complaints_node)
        add_loop(graph, model_node, tools_node)
        graph.add_edge(START, "complaints")
        graph.add_edge("complaints", "model")
        return graph.compile(checkpointer=self.checkpointer, name="nabd_supervisor")

    async def run_turn(self, run: RunContext, tools: McpTools, turn: Turn,
                       on_delta: Callable[[DeltaBuffer], None] | None = None,
                       stage: ComplaintStageCall | None = None) -> str:
        me = await run.activity("supervisor", "agent.supervisor").start()
        deltas = DeltaBuffer(run, assistant_message_id(run.run_id))
        if on_delta:
            on_delta(deltas)
        graph = self.graph(run, tools, turn, deltas, me, stage)
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
                                 "run_id": run.run_id, "tool_calls_made": 0, "case_notes": None}, config)
        await deltas.flush()
        final = await graph.aget_state(config)
        text = reply_text(final.values.get("messages", []), turn.message_id)
        await me.complete()
        return text


def assistant_message_id(run_id: str) -> str:
    """The reply's stable ID, the same for every attempt of a run."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nabd:assistant:{run_id}"))
