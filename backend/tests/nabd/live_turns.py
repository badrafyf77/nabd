"""Harness for the paid scenario runs (``-m llm``): full NABD turns with REAL models.

Each turn runs exactly what the worker runs: the mandatory complaint stage,
then the Supervisor (and the Orders specialist when it delegates), through the
real read and write MCP servers in process.

Data: customers, orders, deliveries and published knowledge come from the
real Salesforce org. Cases come from the seeded fixtures held by
``FakeGateway``, which applies the Apex gateway's rules; so a paid run is
repeatable and leaves no cases behind in the org. The real case write path is
covered by ``python -m nabd write-mcp smoke`` and the browser check.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import httpx2
from langgraph.checkpoint.memory import InMemorySaver

from nabd.agents.cases import ComplaintStage
from nabd.agents.complaints import ChatLine
from nabd.agents.mcp_tools import http_connector
from nabd.agents.models import build_models
from nabd.agents.supervisor import Supervisor, Turn
from nabd.read_mcp.server import ReadBackend, create_app as create_read_app
from nabd.runtime.events import RunContext, TurnBudget
from nabd.salesforce import fixtures as fx
from nabd.salesforce.reads import SalesforceReads
from nabd.salesforce.rest import ClientCredentialsAuth, SalesforceClient
from nabd_fakes import (
    MCP_URL, WRITE_URL, FakeGateway, FixtureReads, MemoryCaseStore, case_token, read_mcp_settings, token,
    write_app, write_token,
)

CASE_NUMBER = re.compile(r"\b0000\d{4}\b")


class HybridReads:
    """Real Salesforce for everything except cases, which come from the fixture world."""

    def __init__(self, real: SalesforceReads, cases: FixtureReads):
        self.real, self.cases = real, cases

    async def list_cases(self, customer_id):
        return await self.cases.list_cases(customer_id)

    async def list_case_history(self, customer_id):
        return await self.cases.list_case_history(customer_id)

    def __getattr__(self, name):
        return getattr(self.real, name)


class Sink:
    def __init__(self):
        self.events = []

    async def emit(self, conversation_id, event):
        self.events.append(event)
        return len(self.events)


@dataclass
class TurnResult:
    reply: str
    events: list
    commands: list  # gateway commands sent this turn
    confirmed: list[str]  # case numbers Salesforce (fake) confirmed this turn

    def completed(self) -> list[str]:
        return [e.code for e in self.events if e.type == "activity" and e.state == "completed"]


@dataclass
class LiveConversation:
    customer: str
    models: object
    salesforce: object
    world: FixtureReads = field(default_factory=FixtureReads)
    history: list[ChatLine] = field(default_factory=list)

    def __post_init__(self):
        self.gateway = FakeGateway(self.world)
        self.store = MemoryCaseStore()
        self.saver = InMemorySaver()
        self.conversation = f"scenario-{uuid.uuid4().hex[:8]}"

    async def say(self, text: str) -> TurnResult:
        sent = len(self.gateway.calls)
        sink = Sink()
        run_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=self.salesforce.timeout_seconds) as http:
            auth = ClientCredentialsAuth(self.salesforce.domain_url, self.salesforce.client_id,
                                         self.salesforce.client_secret, http)
            reads = HybridReads(SalesforceReads(SalesforceClient(auth, http, self.salesforce.api_version)),
                                self.world)
            read_app = create_read_app(read_mcp_settings(), backend=ReadBackend(reads=reads,
                                                                                today=lambda: fx.SEED_DATE))
            writes_app = write_app(self.gateway)
            read = http_connector(MCP_URL, 30, transport=httpx2.ASGITransport(app=read_app))
            write = http_connector(WRITE_URL, 30, transport=httpx2.ASGITransport(app=writes_app))
            run = RunContext(self.conversation, run_id, 1, self.customer, sink, TurnBudget(20))
            ids = {"run_id": run_id, "conversation_id": self.conversation}
            async with read_app.router.lifespan_context(read_app), writes_app.router.lifespan_context(writes_app):
                async with read(token(self.customer, ttl=300, **ids)) as tools, \
                        read(case_token(self.customer, ttl=300, **ids)) as case_reads, \
                        write(write_token(self.customer, ttl=300, **ids)) as writes:
                    index = (await tools.read_json("nabd://knowledge/index"))["entries"]
                    profile = await tools.read_json("nabd://customer/profile")
                    line = ChatLine("customer", message_id, text, now)
                    turn = Turn(message_id, text, profile["first_name"], fx.SEED_DATE, index,
                                (*self.history[-11:], line), now)
                    stage = ComplaintStage(self.models.complaints, self.store)
                    async with asyncio.timeout(120):
                        reply = await Supervisor(self.models.supervisor, self.models.orders, self.saver).run_turn(
                            run, tools, turn, stage=lambda: stage.run(run, turn, case_reads, writes))
        self.history += [line, ChatLine("assistant", f"reply-{run_id}", reply, now)]
        confirmed = [e.payload["result"]["case_number"] for e in sink.events
                     if e.type == "activity" and (e.code or "").startswith("case.") and e.state == "completed"]
        return TurnResult(reply, sink.events, self.gateway.calls[sent:], confirmed)


def live_models(settings):
    return build_models(settings)
