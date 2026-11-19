"""Complaint stage and validated case workflow, with scripted models. Free.

The real read and write MCP servers run in memory over Streamable HTTP; a fake
gateway applies the Apex gateway's rules to fixture cases. Each test checks
what code guarantees: which commands were sent, what Salesforce ended up with,
what the customer's cards show, and what the Supervisor was told.
"""
import asyncio
import json
import re
from datetime import datetime, timezone

import httpx2
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fakes import FakeChatModel, call, say
from nabd.agents.cases import ComplaintStage, build_plan, priority_for, resolve_operation, route
from nabd.agents.complaints import Assessment, Candidate, ChatLine, Fact, Issue
from nabd.agents.mcp_tools import http_connector
from nabd.agents.supervisor import Supervisor, Turn
from nabd.runtime.events import RunContext, TurnBudget
from nabd.salesforce import fixtures as fx
from nabd_fakes import (
    MCP_URL, WRITE_URL, FakeGateway, FixtureReads, MemoryCaseStore, assessment, case_token, issue, mcp_app,
    running, token, tool_json, write_app, write_token,
)

AT = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
MID = "11111111-1111-1111-1111-111111111111"
INTERNAL = ("priority", "sentiment", "urgency", "confidence", "summary", "needs_review", "Medium", "High",
            "risk", "streak", "episode")  # Phase 6: warnings stay in Salesforce


class MemorySink:
    def __init__(self):
        self.events = []

    async def emit(self, conversation_id, event):
        self.events.append(event)
        return len(self.events)

    def cards(self, prefix="case."):
        return [(e.code, e.state) for e in self.events if e.type == "activity" and (e.code or "").startswith(prefix)]

    def last(self, code):
        return [e for e in self.events if e.code == code][-1]


class World:
    """One fake org, gateway and case store shared by the turns of a test."""

    def __init__(self):
        self.reads = FixtureReads()
        self.gateway = FakeGateway(self.reads)
        self.store = MemoryCaseStore()
        self.saver = InMemorySaver()


def reply_with_case_work(messages):
    """A Supervisor reply that copies its case-work section, so tests can read what it was told."""
    system = messages[0].content
    section = system.split("## Case work for this message", 1)[1].split("## What you cannot do", 1)[0]
    return say(section.strip())


async def _turn(world, text, complaints, *, customer, run_id="run-1", message_id=MID, history=(),
                conversation="conv-1", attempt=1, supervisor=None, sink=None):
    sink = sink or MemorySink()
    read_app, writes_app = mcp_app(world.reads), write_app(world.gateway)
    connect_read = http_connector(MCP_URL, 10, transport=httpx2.ASGITransport(app=read_app))
    connect_write = http_connector(WRITE_URL, 10, transport=httpx2.ASGITransport(app=writes_app))
    run = RunContext(conversation, run_id, attempt, customer, sink, TurnBudget(20))
    stage = ComplaintStage(complaints, world.store, 0.6)
    if supervisor is None:
        supervisor = FakeChatModel(default=None)
        supervisor.add(reply_with_case_work)
    agent = Supervisor(supervisor, FakeChatModel(), world.saver)
    lines = (*history, ChatLine("customer", message_id, text, AT))
    async with running(read_app), running(writes_app):
        async with connect_read(token(customer, run_id=run_id, conversation_id=conversation)) as tools, \
                connect_read(case_token(customer, run_id=run_id, conversation_id=conversation)) as case_reads, \
                connect_write(write_token(customer, run_id=run_id, conversation_id=conversation)) as writes:
            index = (await tools.read_json("nabd://knowledge/index"))["entries"]
            turn = Turn(message_id, text, "Sara", fx.SEED_DATE, index, lines, AT)
            reply = await agent.run_turn(run, tools, turn, stage=lambda: stage.run(run, turn, case_reads, writes))
    return reply, sink


def turn(world, text, complaints, **kwargs):
    try:
        return asyncio.run(_turn(world, text, complaints, **kwargs))
    except BaseExceptionGroup as group:  # the in-test server lifespans wrap a crash
        while isinstance(group, BaseExceptionGroup) and len(group.exceptions) == 1:
            group = group.exceptions[0]
        raise group from None


def scripted(*replies):
    model = FakeChatModel()
    model.add(*replies)
    return model


def order_evidence(messages, number=None):
    data = tool_json(messages, "get_order")
    assert number is None or data["order_number"] == number
    return data["evidence"][:2]


def assert_public(sink):
    for event in sink.events:
        if event.type == "activity":
            text = json.dumps(event.payload)
            assert not any(word in text for word in INTERNAL), text


# --- Pure rules ------------------------------------------------------------------------------


def test_priority_policy_follows_the_explicit_rules():
    assert priority_for(["duplicate_charge", "lost_parcel"]) == ("High", "High: suspected duplicate charge")
    assert priority_for(["safety_incident"])[0] == "High"
    assert priority_for(["lost_parcel"]) == ("Medium", "Medium: lost parcel")
    assert priority_for([], reopening=True) == ("Medium", "Medium: repeated unresolved issue")
    assert priority_for([]) == ("Low", "Low: no stronger signal")


def test_uncertain_categories_go_to_general_support_for_review():
    assert route("Billing_Payments", 0.59, 0.6) == ("General_Support", True)
    assert route("Billing_Payments", 0.6, 0.6) == ("Billing_Payments", False)


def _issue(**values):
    base = dict(ref="issue-1", title="t", match="same", action="update", existing_case="00001001",
                category="Delivery", category_confidence=0.9, sentiment="negative", urgency="low",
                urgency_reason="", signals=[], staff_action=None, language="en", order_numbers=[],
                message_ids=[MID], reported=[], facts=[], summary="s", confirmation_message_id=None, question=None)
    return Issue(**{**base, **values})


def _candidate(**values):
    base = dict(case_number="00001001", case_id="500FX0000000001AAA", version="2026-11-18T09:00:00.000Z",
                status="Working", is_closed=False, staff_action=None, staff_action_completed=False,
                order_number="SE-1071", subject="s")
    return Candidate(**{**base, **values})


def test_lifecycle_rules_pick_the_valid_operation():
    fact = [Fact("Delivered.", ["ShopEasy_Delivery__c:x@y"])]
    assert resolve_operation(_issue(match="new", action="create"), None) == "create_case"
    assert resolve_operation(_issue(action="update"), _candidate(is_closed=True, status="Closed")) == "reopen_case"
    assert resolve_operation(_issue(action="reopen"), _candidate()) == "update_case"
    assert resolve_operation(_issue(action="close", facts=fact, confirmation_message_id=MID),
                             _candidate()) == "close_case"
    assert resolve_operation(_issue(action="close", facts=fact), _candidate()) == "request_closure"
    assert resolve_operation(_issue(action="close", confirmation_message_id=MID), _candidate()) == "update_case"
    assert resolve_operation(_issue(action="close", facts=fact, confirmation_message_id=MID),
                             _candidate(staff_action="refund")) == "update_case"
    assert resolve_operation(_issue(action="close"), _candidate(is_closed=True, status="Closed")) is None
    assert resolve_operation(_issue(match="unsure"), _candidate()) is None


def test_two_issues_about_one_case_send_one_command():
    issues = [_issue(ref="issue-1"), _issue(ref="issue-2", title="again")]
    plan = build_plan("run-x", Assessment("issues", issues=issues), {"00001001": _candidate()},
                      {MID: ChatLine("customer", MID, "hi", AT)}, AT, lost_orders=set(), review_threshold=0.6)
    assert [command.operation for command in plan.commands] == ["update_case"]


# --- Complaint flows ---------------------------------------------------------------------------


def test_a_delayed_parcel_opens_a_routed_low_priority_case_and_the_reply_cites_it():
    world = World()
    text = "My cooking pot was supposed to arrive already. Where is it?"
    complaints = scripted(
        call("get_order", "c1", order_number="SE-1061"),
        lambda messages: assessment(issue(
            title="Cast iron cooking pot SE-1061 delayed", order_numbers=["SE-1061"], customer_message_ids=[MID],
            verified_facts=[{"statement": "SE-1061 is delayed; now expected Friday 20 November 2026.",
                             "evidence": order_evidence(messages, "SE-1061")}],
            english_summary="Customer says SE-1061 is late. Delivery is delayed until 20 November.")))
    reply, sink = turn(world, text, complaints, customer="C003")

    [command] = world.gateway.calls
    assert command.operation == "create_case" and command.category == "Delivery"
    assert (command.priority, command.needs_review, command.order_number) == ("Low", False, "SE-1061")
    assert command.issue_id.startswith("nabd-issue-") and command.conversation_id == "conv-1"
    report = next(e for e in command.evidence if e.kind == "customer_report")
    assert (report.ref, report.text) == (MID, text)  # the customer's exact words
    fact = next(e for e in command.evidence if e.kind == "verified_fact")
    assert fact.verified_source.startswith("ShopEasy_Order__c:")
    case = world.reads.case("00002001")
    assert (case.customer_id, case.status, case.queue) == ("C003", "New", "NABD_Delivery")
    assert "Opened case 00002001 with the Delivery team" in reply
    assert sink.cards() == [("case.create", "running"), ("case.create", "waiting"), ("case.create", "completed")]
    done = sink.last("case.create")
    assert done.payload["result"] == {"case_number": "00002001", "team": "Delivery", "case_status": "New",
                                      "operation": "create_case"}
    waiting = next(e for e in sink.events if e.code == "case.create" and e.state == "waiting")
    assert waiting.payload["waiting_for"] == "external_operation" and "case_number" not in json.dumps(waiting.payload)
    complaints_done = sink.last("agent.complaints")
    assert complaints_done.state == "completed" and complaints_done.payload["result"] == {"outcome": "issues",
                                                                                          "issues": 1}
    assert ("tool.list_case_history", "completed") in sink.cards("tool.")
    assert_public(sink)
    assert world.store.links[("conv-1", case.id)]["department"] == "Delivery"


def test_a_lost_parcel_updates_the_customers_existing_case_with_policy_priority():
    world = World()
    complaints = scripted(
        call("get_order", "c1", order_number="SE-1071"),
        lambda messages: assessment(issue(
            title="Smart watch SE-1071 lost", existing_case="00001001", match="same", action="update",
            order_numbers=["SE-1071"], customer_message_ids=[MID], staff_action="carrier_investigation",
            verified_facts=[{"statement": "SE-1071 is marked lost.", "evidence": order_evidence(messages)}])))
    reply, sink = turn(world, "My smart watch never arrived. It's been almost two weeks.", complaints,
                       customer="C004")
    [command] = world.gateway.calls
    assert command.operation == "update_case" and command.case_id == world.reads.case("00001001").id
    assert (command.priority, command.priority_reason) == ("Medium", "Medium: lost parcel")  # from the record
    assert len([c for c in world.reads.cases.values() if c.customer_id == "C004"]) == 1  # no new case
    case = world.reads.case("00001001")
    assert case.status == "Working" and any(key.endswith(f":{MID}") for key in case.evidence)
    assert "Added this message to existing case 00001001" in reply


def test_a_refund_request_becomes_a_returns_and_refunds_staff_case():
    world = World()
    complaints = scripted(assessment(issue(
        title="Refund request for Bluetooth speaker SE-1051", category="Returns_Refunds", staff_action="refund",
        order_numbers=["SE-1051", "SE-1042"], customer_message_ids=[MID], sentiment="neutral")))
    # The Phase 4 scoped index proves SE-1051 belongs to C002; SE-1042 is still excluded.
    reply, _ = turn(world, "I want a refund for my Bluetooth speaker.", complaints, customer="C002")
    [command] = world.gateway.calls
    assert (command.category, command.staff_action, command.order_number) == ("Returns_Refunds", "refund", "SE-1051")
    assert world.reads.case("00002001").queue == "NABD_Returns_Refunds"
    assert "with the Returns & Refunds team" in reply


def test_two_distinct_problems_in_one_message_open_two_cases():
    world = World()
    text = "My cooking pot is late, and the yoga mat I got last month arrived torn."
    complaints = scripted(
        call("list_orders", "c1"),
        lambda messages: assessment(
            issue(title="Cooking pot SE-1061 delayed", order_numbers=["SE-1061"], customer_message_ids=[MID]),
            issue(title="Yoga mat from SE-1062 arrived torn", category="Orders_Products",
                  order_numbers=["SE-1062"], customer_message_ids=[MID], staff_action="replacement")))
    reply, sink = turn(world, text, complaints, customer="C003")
    assert [(c.operation, c.category, c.order_number) for c in world.gateway.calls] == [
        ("create_case", "Delivery", "SE-1061"), ("create_case", "Orders_Products", "SE-1062")]
    assert len({c.issue_id for c in world.gateway.calls}) == 2
    assert [state for code, state in sink.cards() if state == "completed"] == ["completed", "completed"]
    assert "00002001" in reply and "00002002" in reply


def test_an_ordinary_question_creates_no_case():
    world = World()
    reply, sink = turn(world, "How much does delivery cost?", scripted(assessment()), customer="C001")
    assert world.gateway.calls == []
    assert sink.last("agent.complaints").payload["result"] == {"outcome": "no_complaint", "issues": 0}
    assert "No complaint in this message" in reply


def test_closure_needs_verified_evidence_not_just_a_thank_you():
    world = World()
    # The model wrongly proposes closing on a thank-you and cites evidence no tool returned.
    complaints = scripted(assessment(issue(
        title="Plant pot SE-1101", existing_case="00001008", match="same", action="close",
        confirmation_message_id=MID, customer_message_ids=[MID],
        verified_facts=[{"statement": "It was delivered.", "evidence": ["ShopEasy_Delivery__c:invented@x"]}])))
    reply, _ = turn(world, "Ok, thanks.", complaints, customer="C007")
    [command] = world.gateway.calls
    assert command.operation == "update_case"  # recorded, not closed
    assert world.reads.case("00001008").status == "Working"
    assert "no checked record shows the problem is solved" in reply


def test_a_confirmed_resolution_closes_the_case_with_original_confirmation():
    world = World()
    text = "I found the plant pot, my neighbour had taken it in. All sorted, you can close the case."
    complaints = scripted(
        call("get_order", "c1", order_number="SE-1101"),
        lambda messages: assessment(issue(
            title="Plant pot SE-1101 found", existing_case="00001008", match="same", action="close",
            sentiment="positive", confirmation_message_id=MID, customer_message_ids=[MID],
            order_numbers=["SE-1101"],
            verified_facts=[{"statement": "SE-1101 was delivered on Monday 16 November 2026.",
                             "evidence": order_evidence(messages)}])))
    reply, _ = turn(world, text, complaints, customer="C007")
    [command] = world.gateway.calls
    assert command.operation == "close_case" and command.confirmation_message_id == MID
    assert {e.kind for e in command.evidence} == {"customer_confirmation", "verified_fact"}
    case = world.reads.case("00001008")
    assert (case.status, case.resolution, case.confirmation) == ("Closed", "resolved", MID)
    assert next(e for e in case.evidence.values() if e["kind"] == "customer_confirmation")["text"] == text
    assert "Closed case 00001008" in reply


def test_pending_staff_action_keeps_the_case_open():
    world = World()
    complaints = scripted(
        call("get_order", "c1", order_number="SE-1148"),
        lambda messages: assessment(issue(
            title="Boots refund SE-1148", existing_case="00001006", match="same", action="close",
            confirmation_message_id=MID, customer_message_ids=[MID], category="Returns_Refunds",
            verified_facts=[{"statement": "SE-1148 was delivered.", "evidence": order_evidence(messages)}])))
    reply, _ = turn(world, "The refund came through, thanks, you can close it.", complaints, customer="C015")
    assert [c.operation for c in world.gateway.calls] == ["update_case"]
    assert world.reads.case("00001006").status == "Waiting on Staff"
    assert "staff still have to complete the required action" in reply


def test_the_same_problem_after_closure_reopens_the_case():
    world = World()
    complaints = scripted(assessment(issue(
        title="Bed sheets problem is back", existing_case="00001005", match="same", action="update",
        customer_message_ids=[MID])))
    reply, _ = turn(world, "Remember my lost sheets? The replacement is now lost too!", complaints, customer="C014")
    [command] = world.gateway.calls
    assert command.operation == "reopen_case" and command.priority_reason == "Medium: repeated unresolved issue"
    case = world.reads.case("00001005")
    assert (case.status, case.reopen_count, case.staff_action_completed) == ("Working", 1, False)
    assert "Reopened case 00001005" in reply


def test_an_uncertain_match_or_invented_case_asks_instead_of_writing():
    world = World()
    complaints = scripted(assessment(
        issue(existing_case="00001001", match="unsure", question="Is this about your smart watch case?"),
        issue(existing_case="99999999", match="same", action="update", order_numbers=["SE-9999"])))
    reply, _ = turn(world, "It's still not here.", complaints, customer="C004")
    assert world.gateway.calls == []
    assert "Ask the customer: Is this about your smart watch case?" in reply
    assert "Is this about your existing case" not in reply  # the invented case number is not repeated
    assert "Could you tell me a little more" in reply


def test_low_confidence_classification_goes_to_general_support_flagged_for_review():
    world = World()
    complaints = scripted(assessment(issue(category="Billing_Payments", category_confidence=0.3,
                                           customer_message_ids=[MID])))
    turn(world, "Something is wrong with your service.", complaints, customer="C001")
    [command] = world.gateway.calls
    assert (command.category, command.needs_review) == ("General_Support", True)


def test_staff_priority_and_summary_edits_are_respected():
    world = World()
    world.reads.staff_edit("00001001", priority="Low", summary="Staff note: carrier says it was delivered.")
    complaints = scripted(assessment(issue(
        existing_case="00001001", match="same", action="update", priority_signals=["duplicate_charge"],
        english_summary="NABD summary", customer_message_ids=[MID])))
    _, sink = turn(world, "And I was charged twice for it!", complaints, customer="C004")
    [command] = world.gateway.calls
    assert command.priority == "High"  # the policy asks for High ...
    case = world.reads.case("00001001")
    assert case.priority == "Low" and case.summary.startswith("Staff note")  # ... but staff decide
    assert_public(sink)


# --- Reliability -------------------------------------------------------------------------------


def test_a_lost_answer_after_commit_is_reconciled_without_a_second_case():
    world = World()
    world.gateway.fail = ["after"]
    complaints = scripted(assessment(issue(title="Pot late", customer_message_ids=[MID])))
    reply, sink = turn(world, "My pot is late.", complaints, customer="C003")
    assert len(world.gateway.calls) == 1  # looked up by operation ID, never sent twice
    assert len([c for c in world.reads.cases.values() if c.customer_id == "C003"]) == 1
    assert "Opened case 00002001" in reply
    [row] = world.store.commands.values()
    assert (row.status, row.attempts) == ("confirmed", 1)


def test_a_send_that_never_arrived_is_resent_once_with_the_same_operation():
    world = World()
    world.gateway.fail = ["before"]
    turn(world, "My pot is late.", scripted(assessment(issue(customer_message_ids=[MID]))), customer="C003")
    first, second = world.gateway.calls
    assert first.operation_id == second.operation_id and first.body() == second.body()
    assert len([c for c in world.reads.cases.values() if c.customer_id == "C003"]) == 1


def test_an_unanswered_command_stays_pending_and_is_reconciled_later():
    world = World()
    world.gateway.fail = ["before", "before"]
    reply, sink = turn(world, "My pot is late.", scripted(assessment(issue(customer_message_ids=[MID]))),
                       customer="C003")
    assert "Still waiting for Salesforce to confirm" in reply and "00002" not in reply
    [row] = world.store.commands.values()
    assert row.status == "unknown" and sink.cards()[-1] == ("case.create", "waiting")

    async def later():
        app = write_app(world.gateway)
        connect = http_connector(WRITE_URL, 10, transport=httpx2.ASGITransport(app=app))
        run = RunContext("conv-1", "run-1", 0, "C003", sink, TurnBudget(0))
        async with running(app), connect(write_token("C003")) as writes:
            return await ComplaintStage(FakeChatModel(), world.store).reconcile(run, row, writes)

    result = asyncio.run(later())
    assert result.status == "confirmed" and world.store.commands[row.operation_id].status == "confirmed"
    card = sink.last("case.create")
    assert card.state == "completed" and card.activity_id == f"run-1:case:{row.operation_id}"
    assert card.parent_activity_id == "run-1:complaints"


def test_a_retried_run_reuses_its_stored_plan():
    world = World()
    complaints = scripted(assessment(issue(customer_message_ids=[MID])))
    original = world.store.mark_command

    async def crash_once(operation_id, status, **kwargs):
        if status == "confirmed" and not getattr(crash_once, "done", False):
            crash_once.done = True
            raise RuntimeError("the worker died")
        return await original(operation_id, status, **kwargs)

    world.store.mark_command = crash_once
    with pytest.raises(RuntimeError):
        turn(world, "My pot is late.", complaints, customer="C003")
    # Attempt 2: a model that would answer differently is never asked.
    reply, sink = turn(world, "My pot is late.", scripted(), customer="C003", attempt=2)
    assert len(world.gateway.calls) == 1 and "Opened case 00002001" in reply
    assert ("case.create", "completed") in sink.cards()


def test_a_version_conflict_rereads_the_case_and_resends_once():
    world = World()

    def staff_change_then_answer(messages):
        world.reads.staff_edit("00001001", sentiment="neutral")  # staff save while NABD is thinking
        return assessment(issue(existing_case="00001001", match="same", action="update",
                                customer_message_ids=[MID]))

    reply, sink = turn(world, "Any news? Still nothing.", scripted(staff_change_then_answer), customer="C004")
    first, second = world.gateway.calls
    assert first.operation_id != second.operation_id and first.expected_version != second.expected_version
    assert ("case.update", "retrying") in sink.cards() and sink.cards()[-1] == ("case.update", "completed")
    assert "Added this message to existing case 00001001" in reply


def test_a_conflict_that_changes_the_valid_action_is_reported_not_forced():
    world = World()

    def staff_close_then_answer(messages):
        world.reads.staff_edit("00001001", status="Closed", resolution="duplicate")
        return assessment(issue(existing_case="00001001", match="same", action="update",
                                customer_message_ids=[MID]))

    reply, sink = turn(world, "Still nothing.", scripted(staff_close_then_answer), customer="C004")
    assert len(world.gateway.calls) == 1
    assert sink.cards()[-1] == ("case.update", "failed")
    assert "Could not update the case" in reply and "staff changed it meanwhile" in reply


def test_concurrent_runs_of_one_customer_open_one_case():
    world = World()

    def match_if_already_open(messages):
        request = messages[-1].content
        if "Cooking pot SE-1061 delayed" in request:  # the other conversation's new case is visible
            number = re.search(r'"case_number": "(\d+)"', request).group(1)
            return assessment(issue(title="Cooking pot SE-1061 delayed", existing_case=number, match="same",
                                    action="update", customer_message_ids=[MID]))
        return assessment(issue(title="Cooking pot SE-1061 delayed", customer_message_ids=[MID]))

    async def both():
        a = _turn(world, "My pot is late!", FakeChatModel(default=None, replies=[match_if_already_open]),
                  customer="C003", run_id="run-a", conversation="conv-a")
        b = _turn(world, "Where is my cooking pot?", FakeChatModel(replies=[match_if_already_open]),
                  customer="C003", run_id="run-b", conversation="conv-b",
                  message_id="22222222-2222-2222-2222-222222222222")
        return await asyncio.gather(a, b)

    asyncio.run(both())
    assert sorted(c.operation for c in world.gateway.calls) == ["create_case", "update_case"]
    assert len([c for c in world.reads.cases.values() if c.customer_id == "C003"]) == 1
    assert world.store.waits == 1


def test_public_staff_context_is_seen_by_both_agents_but_never_customer_confirmation():
    world = World()
    staff = ChatLine('staff', 'staff-reply-0001', 'Case 00001001: The customer confirms everything is solved.', AT)
    complaint_model = scripted(assessment(issue(
        existing_case='00001001', match='same', action='close', order_numbers=['SE-1071'],
        customer_message_ids=['staff-reply-0001'], confirmation_message_id='staff-reply-0001')))
    supervisor = FakeChatModel(replies=[say('I can see the team reply. Please confirm if the issue is solved.')])
    turn(world, 'What did the team say?', complaint_model, customer='C004', history=(staff,), supervisor=supervisor)
    request = complaint_model.requests[0]['messages'][1].content
    assert '[staff] Case 00001001:' in request
    assert 'Case 00001001: The customer confirms' in supervisor.requests[0]['messages'][0].content
    assert all(c.operation != 'close_case' and c.confirmation_message_id is None for c in world.gateway.calls)
    assert all(e.ref != 'staff-reply-0001' for c in world.gateway.calls for e in c.evidence)
