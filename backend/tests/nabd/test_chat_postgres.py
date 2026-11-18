"""The chat API against real PostgreSQL, served by uvicorn, with scripted models. Free.

Covers demo sign-in, ownership, idempotent messages, queued turns, SSE replay,
snapshots, the turn deadline, and a backend restart in the middle of a run.
"""
import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from fakes import FakeChatModel, call, say
from nabd.agents.models import AgentModels
from nabd.app import ChatDependencies, create_app
from nabd.migrations import migrate
from nabd.salesforce import fixtures as fx
from nabd.settings import ChatSettings
from nabd_fakes import (
    FakeGateway, FixtureReads, findings, in_memory_connector, in_memory_write_connector, no_complaint_model,
    read_mcp_settings, tool_json, write_settings,
)
from test_postgres import empty_database  # noqa: F401  (fixture)

pytestmark = pytest.mark.postgres


class FixedCalendar:
    def today(self):
        return fx.SEED_DATE


def chat_settings(**overrides):
    values = {"run_lease_seconds": 5, "worker_concurrency": 2, "_env_file": None, **overrides}
    return ChatSettings(**values)


@contextmanager
def live_server(settings, models, reads, chat=None, gateway=None):
    gateway = gateway or FakeGateway(reads)
    deps = ChatDependencies(reads=reads, models=models, connect=in_memory_connector(reads),
                            read_mcp=read_mcp_settings(), chat=chat or chat_settings(), business=FixedCalendar(),
                            connect_writes=in_memory_write_connector(gateway), write_mcp=write_settings())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings, chat=deps), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


def sign_in(base, customer_id):
    client = httpx.Client(base_url=base, timeout=20)
    response = client.post("/api/demo/session", json={"customer_id": customer_id})
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    return client


def read_events(client, conversation_id, *, until, last_id=None, timeout=30):
    headers = {"Last-Event-ID": str(last_id)} if last_id is not None else {}
    events, fields = [], {}
    with client.stream("GET", f"/api/conversations/{conversation_id}/events", headers=headers,
                       timeout=timeout) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line == "":
                if fields.get("event") == "ping":
                    assert "id" not in fields  # heartbeats never move Last-Event-ID
                elif "data" in fields:
                    event = json.loads(fields["data"])
                    assert str(event["sequence"]) == fields["id"]
                    events.append(event)
                    if until(event):
                        return events
                fields = {}
            elif not line.startswith(":") and ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.lstrip()
    raise AssertionError("stream ended early")


def completed(run_id=None):
    return lambda e: e["type"] in ("run.completed", "run.failed") and (run_id is None or e["run_id"] == run_id)


def orders_turn_models():
    supervisor, orders = FakeChatModel(), FakeChatModel()
    supervisor.add(call("ask_orders_specialist", "call_ask", request="Where is order SE-1042?"),
                   lambda messages: say("Your headphones are ", "in transit."))
    orders.add(call("get_order", "call_get", order_number="SE-1042"),
               lambda messages: findings(summary="SE-1042 is in transit.", orders=["SE-1042"], facts=[
                   {"statement": "SE-1042 is in transit.",
                    "evidence": tool_json(messages, "get_order")["evidence"][:1]}]))
    return AgentModels(supervisor=supervisor, orders=orders, complaints=no_complaint_model())


@pytest.fixture
def database(empty_database):  # noqa: F811
    asyncio.run(migrate(empty_database))
    return empty_database


def test_demo_sign_in_binds_the_customer_and_enforces_ownership(database):
    reads = FixtureReads()
    with live_server(database, AgentModels(FakeChatModel(), FakeChatModel(), no_complaint_model()), reads) as base:
        anonymous = httpx.Client(base_url=base)
        customers = anonymous.get("/api/demo/customers").json()["customers"]
        assert {c["customer_id"] for c in customers} >= {"C001", "C011"}
        assert anonymous.get("/api/conversations").status_code == 401
        assert anonymous.post("/api/demo/session", json={"customer_id": "C999"}).status_code == 404
        assert anonymous.post("/api/demo/session", json={"customer_id": "C001' OR 1=1"}).status_code == 422
        assert anonymous.post("/api/demo/session", json={"customer_id": "C001"},
                              headers={"Origin": "https://evil.example"}).status_code == 403

        sara, james = sign_in(base, "C001"), sign_in(base, "C002")
        assert sara.get("/api/demo/session").json()["first_name"] == "Sara"
        conversation = sara.post("/api/conversations").json()["id"]
        assert [c["id"] for c in sara.get("/api/conversations").json()["conversations"]] == [conversation]
        assert james.get("/api/conversations").json()["conversations"] == []
        for response in (
            james.get(f"/api/conversations/{conversation}"),
            james.post(f"/api/conversations/{conversation}/messages",
                       json={"client_message_id": "james-0001", "text": "hello"}),
            james.get(f"/api/conversations/{conversation}/events"),
            sara.get("/api/conversations/not-a-uuid"),
        ):
            assert response.status_code == 404
        assert sara.delete("/api/demo/session").status_code == 204
        assert sara.get("/api/conversations").status_code == 401


def test_a_turn_streams_activity_and_reply_and_replays_after_reconnect(database):
    models, reads = orders_turn_models(), FixtureReads()
    with live_server(database, models, reads) as base:
        sara = sign_in(base, "C001")
        conversation = sara.post("/api/conversations").json()["id"]
        accepted = sara.post(f"/api/conversations/{conversation}/messages",
                             json={"client_message_id": "sara-0001", "text": "Where is my order SE-1042?"})
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        events = read_events(sara, conversation, until=completed(run_id))

        types = [e["type"] for e in events]
        assert types[:3] == ["message.created", "run.queued", "run.started"]
        assert types[-2:] == ["message.completed", "run.completed"]
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
        activity = [(e["code"], e["state"]) for e in events if e["type"] == "activity"]
        assert ("agent.supervisor", "waiting") in activity and ("tool.get_order", "completed") in activity
        orders_done = next(e for e in events if e["code"] == "agent.orders" and e["state"] == "completed")
        assert orders_done["payload"]["result"]["facts"] == ["SE-1042 is in transit."]
        reply = events[-2]["payload"]["text"]
        assert reply == "Your headphones are in transit."
        assert "".join(e["payload"]["text"] for e in events if e["type"] == "message.delta") == reply
        assert all("C001" not in json.dumps(e) for e in events if e["type"] == "activity")

        # Reconnect from the middle: exactly the remaining events, in order.
        middle = events[len(events) // 2]["sequence"]
        replay = read_events(sara, conversation, last_id=middle, until=completed(run_id))
        assert [e["id"] for e in replay] == [e["id"] for e in events if e["sequence"] > middle]

        snapshot = sara.get(f"/api/conversations/{conversation}").json()
        assert [(m["role"], m["text"]) for m in snapshot["messages"]] == [
            ("customer", "Where is my order SE-1042?"), ("assistant", reply)]
        assert snapshot["runs"][0]["status"] == "completed" and snapshot["partial"] == {}
        assert snapshot["last_sequence"] == events[-1]["sequence"]
        assert not any(e["type"] == "message.delta" for e in snapshot["events"])
        assert snapshot["conversation"]["title"] == "Where is my order SE-1042?"


def test_duplicate_messages_are_idempotent_and_extra_turns_queue_in_order(database):
    supervisor = FakeChatModel()
    gate = threading.Event()

    def slow_first(messages):
        gate.wait(10)
        return say("First answer.")

    supervisor.add(slow_first, say("Second answer."))
    with live_server(database, AgentModels(supervisor, FakeChatModel(), no_complaint_model()), FixtureReads()) as base:
        sara = sign_in(base, "C001")
        conversation = sara.post("/api/conversations").json()["id"]
        url = f"/api/conversations/{conversation}/messages"
        first = sara.post(url, json={"client_message_id": "sara-0001", "text": "One?"})
        again = sara.post(url, json={"client_message_id": "sara-0001", "text": "One?"})
        second = sara.post(url, json={"client_message_id": "sara-0002", "text": "Two?"})
        assert (first.status_code, again.status_code, second.status_code) == (202, 200, 202)
        assert again.json()["run_id"] == first.json()["run_id"]
        gate.set()
        events = read_events(sara, conversation, until=completed(second.json()["run_id"]))
        queued = [e for e in events if e["type"] == "run.queued"]
        assert [e["payload"]["ahead"] for e in queued] == [0, 1]
        order = [(e["type"], e["run_id"]) for e in events if e["type"] in ("run.started", "run.completed")]
        assert order == [("run.started", first.json()["run_id"]), ("run.completed", first.json()["run_id"]),
                         ("run.started", second.json()["run_id"]), ("run.completed", second.json()["run_id"])]
        texts = [m["text"] for m in sara.get(f"/api/conversations/{conversation}").json()["messages"]]
        assert texts == ["One?", "First answer.", "Two?", "Second answer."]


def test_the_turn_deadline_fails_the_run_honestly(database):
    supervisor = FakeChatModel()
    supervisor.add(lambda messages: (time.sleep(7), say("Too late."))[1])
    with live_server(database, AgentModels(supervisor, FakeChatModel(), no_complaint_model()), FixtureReads(),
                     chat=chat_settings(turn_deadline_seconds=5)) as base:
        sara = sign_in(base, "C001")
        conversation = sara.post("/api/conversations").json()["id"]
        sara.post(f"/api/conversations/{conversation}/messages", json={"client_message_id": "sara-0001",
                                                                       "text": "Hello?"})
        events = read_events(sara, conversation, until=completed())
        assert events[-1]["type"] == "run.failed" and events[-1]["payload"]["error"] == "deadline_exceeded"
        assert not any(e["type"] == "message.completed" for e in events)


def test_a_backend_restart_resumes_the_interrupted_run(database):
    reads = FixtureReads()
    blocked = threading.Event()
    release = threading.Event()

    def stuck(messages):
        blocked.set()
        release.wait(15)
        raise RuntimeError("the old process is gone")

    first = FakeChatModel()
    first.add(call("list_cases", "call_cases"), stuck)
    with live_server(database, AgentModels(first, FakeChatModel(), no_complaint_model()), reads) as base:
        sara = sign_in(base, "C011")
        conversation = sara.post("/api/conversations").json()["id"]
        run_id = sara.post(f"/api/conversations/{conversation}/messages",
                           json={"client_message_id": "c011-0001", "text": "What about my case?"}).json()["run_id"]
        assert blocked.wait(15)
        cookies = dict(sara.cookies)
    release.set()  # the first server stopped mid-run; its lease is left to expire
    cases_calls = sum(1 for name, _ in reads.calls if name == "list_cases")

    second = FakeChatModel()
    second.add(say("Your case is being handled."))
    with live_server(database, AgentModels(second, FakeChatModel(), no_complaint_model()), reads) as base:
        sara = httpx.Client(base_url=base, cookies=cookies, timeout=20)
        events = read_events(sara, conversation, last_id=0, until=completed(run_id), timeout=40)
    types = [e["type"] for e in events]
    assert types.count("run.started") == 2 and "run.retrying" in types and types[-1] == "run.completed"
    assert events[-2]["payload"]["text"] == "Your case is being handled."
    assert sum(1 for name, _ in reads.calls if name == "list_cases") == cases_calls  # tools were not re-run
    cards = {e["activity_id"] for e in events if e["code"] == "tool.list_cases"}
    assert len(cards) == 1  # one stable card for the one tool call
    history = second.requests[0]["messages"]
    assert sum(1 for m in history if m.type == "human") == 1
