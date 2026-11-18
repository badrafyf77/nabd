"""NABD Supervisor and Orders over the real read MCP server, with scripted models. Free."""
import asyncio
import json

import httpx2
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakes import FakeChatModel, call, say
from nabd.agents.mcp_tools import http_connector
from nabd.agents.orders import verify_findings
from nabd.agents.supervisor import Supervisor, Turn, assistant_message_id
from nabd.runtime.events import RunContext, TurnBudget
from nabd.salesforce import fixtures as fx
from nabd_fakes import MCP_URL, FixtureReads, mcp_app, running, token, unavailable


class MemorySink:
    def __init__(self):
        self.events = []

    async def emit(self, conversation_id, event):
        self.events.append(event)
        return len(self.events)

    def activities(self, code=None):
        return [(e.code, e.state) for e in self.events if e.type == "activity" and (code is None or e.code == code)]


def tool_json(messages, name):
    """The parsed result of the most recent tool call ``name`` the model saw."""
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == name:
            return json.loads(message.content)
    raise AssertionError(f"no {name} result")


def findings(outcome="done", summary="", facts=(), orders=(), questions=()):
    return say(json.dumps({"outcome": outcome, "summary": summary, "facts": list(facts),
                           "order_numbers": list(orders), "questions": list(questions)}))


async def run_turn(text, supervisor_model, orders_model, *, customer="C001", reads=None, checkpointer=None,
                   run_id="run-1", sink=None, budget=20, message_id="11111111-1111-1111-1111-111111111111"):
    reads = reads or FixtureReads()
    sink = sink or MemorySink()
    app = mcp_app(reads)
    connect = http_connector(MCP_URL, 10, transport=httpx2.ASGITransport(app=app))
    run = RunContext("conv-1", run_id, 1, customer, sink, TurnBudget(budget))
    supervisor = Supervisor(supervisor_model, orders_model, checkpointer or InMemorySaver())
    try:
        async with running(app), connect(token(customer, run_id=run_id)) as tools:
            index = (await tools.read_json("nabd://knowledge/index"))["entries"]
            turn = Turn(message_id, text, "Sara", fx.SEED_DATE, index)
            reply = await supervisor.run_turn(run, tools, turn)
    except BaseExceptionGroup as group:  # the in-test MCP server lifespan wraps a crash once more
        while isinstance(group, BaseExceptionGroup) and len(group.exceptions) == 1:
            group = group.exceptions[0]
        raise group from None
    return reply, sink, reads


def test_supervisor_awaits_orders_and_shows_only_verified_findings():
    supervisor = FakeChatModel()
    supervisor.add(call("ask_orders_specialist", "call_ask", request="Where is order SE-1042?"),
                   lambda messages: say("Your headphones (SE-1042) are ",
                                        tool_json(messages, "ask_orders_specialist")["summary"]))
    orders = FakeChatModel()

    def answer(messages):
        order = tool_json(messages, "get_order")
        return findings(summary="in transit with DPD.", orders=["SE-1042", "SE-9999"], facts=[
            {"statement": "SE-1042 is in transit.", "evidence": order["evidence"][:1]},
            {"statement": "It will arrive tomorrow.", "evidence": ["ShopEasy_Order__c:invented@x"]},
        ])

    orders.add(call("get_order", "call_get", order_number="SE-1042"), answer)

    reply, sink, reads = asyncio.run(run_turn("Where is my order SE-1042?", supervisor, orders))

    assert reply == "Your headphones (SE-1042) are in transit with DPD."
    assert ("get_order", ("C001", "SE-1042")) in reads.calls
    assert sink.activities() == [
        ("agent.supervisor", "running"), ("agent.orders", "running"), ("agent.supervisor", "waiting"),
        ("tool.get_order", "running"), ("tool.get_order", "completed"), ("agent.orders", "completed"),
        ("agent.supervisor", "running"), ("agent.supervisor", "completed"),
    ]
    by_code = {e.code: e for e in sink.events if e.type == "activity" and e.state in ("completed", "waiting")}
    assert by_code["agent.supervisor"].payload  # waiting names its dependency below
    waiting = next(e for e in sink.events if e.state == "waiting")
    assert waiting.payload["waiting_for"] == "dependency"
    assert waiting.payload["waiting_on"] == by_code["agent.orders"].activity_id
    tool = by_code["tool.get_order"]
    assert tool.parent_activity_id == by_code["agent.orders"].activity_id
    assert tool.payload["params"] == {"order_number": "SE-1042"}
    assert tool.payload["result"] == {"order_number": "SE-1042", "found": True}
    public = by_code["agent.orders"].payload["result"]
    assert public["facts"] == ["SE-1042 is in transit."]  # the invented citation is not shown
    assert public["order_numbers"] == ["SE-1042"]  # an order no tool returned is dropped
    sent = tool_json(supervisor.requests[1]["messages"], "ask_orders_specialist")
    assert sent["unverified_statements"] == ["It will arrive tomorrow."]
    deltas = "".join(e.payload["text"] for e in sink.events if e.type == "message.delta")
    assert deltas == reply
    assert {e.payload["message_id"] for e in sink.events if e.type == "message.delta"} == {
        assistant_message_id("run-1")}
    # The Supervisor's prompt carries the published knowledge index, never the customer ID.
    system = supervisor.requests[0]["messages"][0].content
    assert "faq.delivery_cost" in system and "C001" not in system


def test_the_model_cannot_use_tools_outside_its_allowlist():
    supervisor = FakeChatModel()
    supervisor.add(call("get_order", "call_direct", order_number="SE-1051"), say("I can't check that."))
    reply, sink, reads = asyncio.run(run_turn("Status of SE-1051?", supervisor, FakeChatModel()))
    assert ("tool.get_order", "failed") in sink.activities()
    assert not any(name == "get_order" for name, _ in reads.calls)
    result = tool_json(supervisor.requests[1]["messages"], "get_order")
    assert result["error"] == "not_allowed"


def test_knowledge_answers_cite_the_published_revision():
    supervisor = FakeChatModel()
    supervisor.add(call("get_knowledge", "call_kb", entry_keys=["faq.delivery_cost"]),
                   lambda messages: say(tool_json(messages, "get_knowledge")["entries"][0]["body"]))
    reply, sink, _ = asyncio.run(run_turn("How much is delivery?", supervisor, FakeChatModel()))
    assert "£3.99" in reply
    done = next(e for e in sink.events if e.code == "tool.get_knowledge" and e.state == "completed")
    assert done.payload["result"]["articles"][0]["revision"] == "faq.delivery_cost@2"


def test_shared_turn_budget_switches_tools_off():
    supervisor = FakeChatModel()
    supervisor.add(call("list_cases", "c1"), call("list_cases", "c2"), say("Here is what I found."))
    reply, sink, reads = asyncio.run(run_turn("My cases?", supervisor, FakeChatModel(), budget=1))
    assert sum(1 for name, _ in reads.calls if name == "list_cases") == 1
    assert tool_json(supervisor.requests[2]["messages"], "list_cases")["error"] == "limit_reached"
    assert supervisor.requests[2]["tool_choice"] == "none"


def test_salesforce_outage_is_retried_once_and_shown():
    reads = FixtureReads()
    reads.fail_on["list_cases"] = [unavailable()]
    supervisor = FakeChatModel()
    supervisor.add(call("list_cases", "c1"), say("You have one case."))
    reply, sink, _ = asyncio.run(run_turn("My cases?", supervisor, FakeChatModel(), reads=reads, customer="C011"))
    assert sink.activities("tool.list_cases") == [("tool.list_cases", "running"), ("tool.list_cases", "retrying"),
                                                  ("tool.list_cases", "completed")]


def test_a_retried_run_resumes_the_checkpoint_without_repeating_work():
    saver = InMemorySaver()
    supervisor = FakeChatModel()
    supervisor.add(call("list_cases", "c1"))  # the second model call never happens: the worker died
    supervisor.add(lambda messages: (_ for _ in ()).throw(RuntimeError("worker crashed")))
    reads = FixtureReads()
    with pytest.raises(RuntimeError):
        asyncio.run(run_turn("My cases?", supervisor, FakeChatModel(), reads=reads, checkpointer=saver,
                             customer="C011"))
    calls_before = len([c for c in reads.calls if c[0] == "list_cases"])

    supervisor.add(say("You have one open case."))
    reply, sink, _ = asyncio.run(run_turn("My cases?", supervisor, FakeChatModel(), reads=reads, checkpointer=saver,
                                          customer="C011"))
    assert reply == "You have one open case."
    assert len([c for c in reads.calls if c[0] == "list_cases"]) == calls_before  # tools not re-run
    history = supervisor.requests[-1]["messages"]
    assert sum(isinstance(m, HumanMessage) for m in history) == 1


def test_a_new_turn_after_an_abandoned_run_closes_its_tool_calls():
    saver = InMemorySaver()
    supervisor = FakeChatModel()
    supervisor.add(lambda messages: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        asyncio.run(run_turn("First?", supervisor, FakeChatModel(), checkpointer=saver, run_id="run-a"))
    supervisor.add(call("list_cases", "c1"))
    supervisor.add(lambda messages: (_ for _ in ()).throw(RuntimeError("boom again")))
    with pytest.raises(RuntimeError):
        asyncio.run(run_turn("Second?", supervisor, FakeChatModel(), checkpointer=saver, run_id="run-b",
                             message_id="22222222-2222-2222-2222-222222222222"))
    supervisor.add(say("Done."))
    reply, _, _ = asyncio.run(run_turn("Third?", supervisor, FakeChatModel(), checkpointer=saver, run_id="run-c",
                                       message_id="33333333-3333-3333-3333-333333333333"))
    assert reply == "Done."
    history = supervisor.requests[-1]["messages"]
    calls = {c["id"] for m in history if hasattr(m, "tool_calls") for c in m.tool_calls}
    assert calls <= {m.tool_call_id for m in history if isinstance(m, ToolMessage)}


def test_findings_without_valid_json_cannot_complete():
    result = verify_findings("not json", set(), set(), 2)
    assert result.outcome == "cannot_complete" and result.tool_calls == 2
