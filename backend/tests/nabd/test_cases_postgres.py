"""Case work against real PostgreSQL, with scripted models and the fake gateway. Free.

Covers the durable plan across a crashed attempt, the per-customer advisory
lock across two runs, background reconciliation of unknown outcomes, and the
chat API's case cards and snapshot.
"""
import asyncio
import json
import re

import pytest

from fakes import FakeChatModel, call, say
from nabd.agents.cases import ComplaintStage
from nabd.agents.models import AgentModels
from nabd.database import open_database
from nabd.migrations import migrate
from nabd.runtime.store import Store
from nabd.runtime.worker import RunWorker, TurnExecutor
from nabd_fakes import (
    FakeGateway, FixtureReads, assessment, in_memory_write_connector, issue, read_mcp_settings, tool_json,
    write_settings,
)
from test_chat_postgres import chat_settings, completed, live_server, read_events, sign_in
from test_complaints import World, _turn
from test_postgres import empty_database  # noqa: F401  (fixture)

pytestmark = pytest.mark.postgres
# Phase 6 adds risk warnings and the negative-contact streak: Salesforce-only, never customer activity.
INTERNAL = ("priority", "sentiment", "urgency", "confidence", "summary", "needs_review", "risk", "streak", "episode")


@pytest.fixture
def database(empty_database):  # noqa: F811
    asyncio.run(migrate(empty_database))
    return empty_database


async def _conversation(store, customer, texts):
    """A stored conversation with one queued run per message: (conversation ID, [(run, message)])."""
    conversation = (await store.create_conversation(customer, "Test"))["id"]
    accepted = []
    for index, text in enumerate(texts):
        result, _ = await store.submit_message(conversation, f"client-{customer}-{index:04d}", text)
        accepted.append((result["run_id"], result["message_id"]))
    return conversation, accepted


def _pg_world(database_pool, settings):
    world = World()
    world.store = Store(database_pool, lock_conninfo=settings.database_url.get_secret_value())
    return world


def test_a_crashed_attempt_keeps_its_plan_and_the_retry_sends_nothing_twice(database):
    async def scenario():
        async with open_database(database) as db:
            world = _pg_world(db.pool, database)
            conversation, [(run_id, message_id)] = await _conversation(world.store, "C003", ["My pot is late."])
            original = world.store.mark_command
            crashed = []

            async def crash_once(operation_id, status, **kwargs):
                if status == "confirmed" and not crashed:
                    crashed.append(operation_id)
                    raise RuntimeError("the worker died after Salesforce committed")
                return await original(operation_id, status, **kwargs)

            world.store.mark_command = crash_once
            complaints = FakeChatModel(replies=[assessment(issue(title="Pot SE-1061 late",
                                                                 customer_message_ids=[message_id]))])
            with pytest.raises(Exception):
                await _turn(world, "My pot is late.", complaints, customer="C003", run_id=run_id,
                            conversation=conversation, message_id=message_id, sink=world.store)
            plan, rows = await world.store.case_plan(run_id)
            assert plan["assessment"]["outcome"] == "issues" and [r.status for r in rows] == ["dispatched"]

            reply, _ = await _turn(world, "My pot is late.", FakeChatModel(), customer="C003", run_id=run_id,
                                   conversation=conversation, message_id=message_id, attempt=2, sink=world.store)
            assert len(world.gateway.calls) == 1 and "Opened case 00002001" in reply
            _, rows = await world.store.case_plan(run_id)
            assert [(r.status, r.attempts) for r in rows] == [("confirmed", 1)]
            snapshot = await world.store.snapshot(conversation)
            assert [(c["case_number"], c["department"]) for c in snapshot["cases"]] == [("00002001", "Delivery")]

    asyncio.run(scenario())


def test_the_advisory_lock_lets_a_concurrent_run_see_the_new_case(database):
    async def scenario():
        async with open_database(database) as db:
            world = _pg_world(db.pool, database)
            conv_a, [(run_a, msg_a)] = await _conversation(world.store, "C003", ["My pot is late!"])
            conv_b, [(run_b, msg_b)] = await _conversation(world.store, "C003", ["Where is my cooking pot?"])

            def match_if_open(messages):
                request = messages[-1].content
                found = re.search(r'"case_number": "(\d+)", "subject": "Cooking pot SE-1061 delayed"', request)
                message = re.search(r"Current customer message: \[customer ([0-9a-f-]+)\]", request).group(1)
                if found:
                    return assessment(issue(title="Cooking pot SE-1061 delayed", existing_case=found.group(1),
                                            match="same", action="update", customer_message_ids=[message]))
                return assessment(issue(title="Cooking pot SE-1061 delayed", customer_message_ids=[message]))

            await asyncio.gather(
                _turn(world, "My pot is late!", FakeChatModel(replies=[match_if_open]), customer="C003",
                      run_id=run_a, conversation=conv_a, message_id=msg_a, sink=world.store),
                _turn(world, "Where is my cooking pot?", FakeChatModel(replies=[match_if_open]), customer="C003",
                      run_id=run_b, conversation=conv_b, message_id=msg_b, sink=world.store))
            assert sorted(c.operation for c in world.gateway.calls) == ["create_case", "update_case"]
            assert len([c for c in world.reads.cases.values() if c.customer_id == "C003"]) == 1
            waits = [e for conv in (conv_a, conv_b) for e in await world.store.events_after(conv, 0)
                     if e["code"] == "agent.complaints" and e["state"] == "waiting"]
            assert len(waits) == 1 and waits[0]["payload"]["waiting_reason"] == "other_conversation"

    asyncio.run(scenario())


def test_the_worker_reconciles_unknown_outcomes_after_the_run_ended(database):
    async def scenario():
        async with open_database(database) as db:
            world = _pg_world(db.pool, database)
            world.gateway.fail = ["before", "before"]
            conversation, [(run_id, message_id), (run_2, message_2)] = await _conversation(
                world.store, "C003", ["My pot is late.", "And my yoga mat is torn."])
            reply, _ = await _turn(world, "My pot is late.",
                                   FakeChatModel(replies=[assessment(issue(customer_message_ids=[message_id]))]),
                                   customer="C003", run_id=run_id, conversation=conversation,
                                   message_id=message_id, sink=world.store)
            assert "Still waiting for Salesforce" in reply
            # A second run whose command was stored but never sent before its run ended.
            plan_row = {"operation_id": "op-never-sent-0001", "operation": "create_case"}
            from nabd.runtime.store import NewCommand
            await world.store.save_case_plan(run_2, conversation, "C003", {"assessment": {"outcome": "issues"}},
                                             [NewCommand("op-never-sent-0001", "1", "issue-1", "create_case",
                                                         plan_row, {"title": "Mat"})])
            for _ in range(2):  # both runs end (the turn deadline, say)
                claimed = await world.store.claim_run("w", 30, 3)
                await world.store.fail_run(claimed, "deadline_exceeded")

            settings = chat_settings(reconcile_after_seconds=1)
            stage = ComplaintStage(FakeChatModel(), world.store)
            executor = TurnExecutor(None, None, read_mcp_settings(), settings, None, stage,
                                    in_memory_write_connector(world.gateway), write_settings())
            worker = RunWorker(world.store, executor, settings)
            await asyncio.sleep(1.1)
            assert await worker.reconcile_once() == 2
            _, [row] = await world.store.case_plan(run_id)
            assert row.status == "confirmed" and world.reads.case("00002001").customer_id == "C003"
            _, [abandoned] = await world.store.case_plan(run_2)
            assert (abandoned.status, abandoned.error_code) == ("failed", "abandoned")
            assert len(world.gateway.calls) == 3  # two unanswered sends, one reconciled send; none for run 2
            cards = [(e["run_id"], e["state"]) for e in await world.store.events_after(conversation, 0)
                     if (e["code"] or "").startswith("case.")]
            assert cards[-2:] in ([(run_id, "completed"), (run_2, "failed")], [(run_2, "failed"), (run_id, "completed")])
            assert await worker.reconcile_once() == 0

    asyncio.run(scenario())


def _complaint_models():
    complaints = FakeChatModel()
    complaints.add(
        call("get_order", "c1", order_number="SE-1061"),
        lambda messages: assessment(issue(
            title="Cast iron cooking pot SE-1061 delayed", order_numbers=["SE-1061"],
            customer_message_ids=[re.search(r"Current customer message: \[customer ([0-9a-f-]+)\]",
                                            messages[1].content).group(1)],
            verified_facts=[{"statement": "SE-1061 is delayed.",
                             "evidence": tool_json(messages, "get_order")["evidence"][:2]}])))
    return complaints


def test_a_complaint_turn_streams_case_cards_and_the_snapshot_lists_the_case(database):
    def reply(messages):
        number = re.search(r"Opened case (\d+)", messages[0].content).group(1)
        return say(f"I've recorded this as case {number} with our Delivery team.")

    supervisor, complaints = FakeChatModel(replies=[reply]), _complaint_models()
    reads = FixtureReads()
    with live_server(database, AgentModels(supervisor, FakeChatModel(), complaints), reads) as base:
        amina = sign_in(base, "C003")
        conversation = amina.post("/api/conversations").json()["id"]
        accepted = amina.post(f"/api/conversations/{conversation}/messages",
                              json={"client_message_id": "amina-0001", "text": "My cooking pot is late."}).json()
        events = read_events(amina, conversation, until=completed(accepted["run_id"]))
        snapshot = amina.get(f"/api/conversations/{conversation}").json()

    cards = [(e["code"], e["state"]) for e in events if e["type"] == "activity" and e["code"].startswith("case.")]
    assert cards == [("case.create", "running"), ("case.create", "waiting"), ("case.create", "completed")]
    done = next(e for e in events if e["code"] == "case.create" and e["state"] == "completed")
    assert done["payload"]["result"]["case_number"] == "00002001" and done["payload"]["result"]["team"] == "Delivery"
    assert done["parent_activity_id"] == f"{accepted['run_id']}:complaints"
    for event in events:
        if event["type"] == "activity":
            assert not any(word in json.dumps(event["payload"]) for word in INTERNAL)
    assert events[-2]["payload"]["text"] == "I've recorded this as case 00002001 with our Delivery team."
    assert snapshot["cases"] == [{"case_number": "00002001", "department": "Delivery", "status": "New",
                                  "last_operation": "create_case", "updated_at": snapshot["cases"][0]["updated_at"]}]
    # The specialist saw the stored message ID, which became the evidence reference.
    assert f"[customer {accepted['message_id']}]" in complaints.requests[0]["messages"][1].content
    assert reads.case("00002001").evidence  # original wording stored on the case


def test_a_retry_after_case_work_resumes_the_reply_without_repeating_it(database):
    supervisor = FakeChatModel()
    supervisor.add(lambda messages: (_ for _ in ()).throw(RuntimeError("model outage")),
                   say("Your case is open."))
    complaints = _complaint_models()
    reads = FixtureReads()
    gateway = FakeGateway(reads)
    with live_server(database, AgentModels(supervisor, FakeChatModel(), complaints), reads, gateway=gateway) as base:
        amina = sign_in(base, "C003")
        conversation = amina.post("/api/conversations").json()["id"]
        run_id = amina.post(f"/api/conversations/{conversation}/messages",
                            json={"client_message_id": "amina-0001", "text": "My pot is late."}).json()["run_id"]
        events = read_events(amina, conversation, until=completed(run_id), timeout=40)
    types = [e["type"] for e in events]
    assert "run.retrying" in types and types[-1] == "run.completed"
    assert len(gateway.calls) == 1 and len(complaints.requests) == 2  # one investigation (tool call + answer)
    assert [e["state"] for e in events if e["code"] == "case.create"].count("completed") == 1
