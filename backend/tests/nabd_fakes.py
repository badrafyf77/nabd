"""Test doubles for the NABD chat runtime. No network, no cost.

``FixtureReads`` answers like ``SalesforceReads`` but from the Phase 1 fixture
dataset, so MCP and agent tests run against realistic ShopEasy records.
``read_mcp_client`` connects a real MCP client to the real read server over
Streamable HTTP, carried in memory by an ASGI transport.
"""
from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from nabd import context
from nabd.read_mcp.server import ReadBackend, create_app
from nabd.salesforce import fixtures as fx
from nabd.salesforce import reads as sf
from nabd.salesforce.rest import SalesforceUnavailable
from nabd.salesforce.soql import CUSTOMER_ID, KNOWLEDGE_KEY, ORDER_NUMBER, require
from nabd.settings import ReadMcpSettings

SECRET = SecretStr("test-context-secret-that-is-long-enough")
MCP_URL = "http://127.0.0.1:8091/mcp"
STAMP = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


class FixtureReads:
    """``SalesforceReads`` stand-in over ``fixtures.build()``; records every call."""

    def __init__(self, dataset: fx.Dataset | None = None):
        self.dataset = dataset or fx.build()
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next: list[Exception] = []
        self.fail_on: dict[str, list[Exception]] = {}  # adapter method name -> errors to raise next
        self._ids = itertools.count(1)
        self._refs: dict[tuple[str, str], sf.RecordRef] = {}

    def _ref(self, sobject: str, key: str) -> sf.RecordRef:
        if (sobject, key) not in self._refs:
            self._refs[(sobject, key)] = sf.RecordRef(sobject=sobject, id=f"a0X{next(self._ids):012d}AAA",
                                                      version=STAMP)
        return self._refs[(sobject, key)]

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, args))
        if self.fail_on.get(name):
            raise self.fail_on[name].pop(0)
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _order(self, order: fx.Order) -> sf.Order:
        d = order.delivery
        return sf.Order(
            order_number=order.order_number, order_date=order.order_date, status=order.status,
            total_gbp=order.total, payment_method=order.payment_method,
            delivery=sf.Delivery(
                status=d.status, carrier=d.carrier, tracking_number=d.tracking_number, shipped_date=d.shipped,
                expected_date=d.expected, original_expected_date=d.original_expected, delivered_date=d.delivered,
                last_location=d.last_location, last_update_date=d.last_update,
                record=self._ref("ShopEasy_Delivery__c", order.order_number)),
            items=tuple(sf.OrderItem(
                line_number=i.line_number, product=i.product, category=i.category, quantity=i.quantity,
                unit_price_gbp=i.unit_price, returnable=i.returnable,
                record=self._ref("ShopEasy_Order_Item__c", f"{order.order_number}-{i.line_number}"))
                for i in order.items),
            record=self._ref("ShopEasy_Order__c", order.order_number),
        )

    def _knowledge(self, entry: fx.Knowledge) -> sf.KnowledgeRevision:
        return sf.KnowledgeRevision(
            entry_key=entry.entry_key, revision=entry.revision, revision_key=entry.revision_key,
            entry_type=entry.entry_type, title=entry.title, question=entry.question, body=entry.body,
            topic=entry.topic, language="en", published_at=entry.published_at, policy_days=entry.policy_days,
            record=self._ref("KnowledgeEntry__c", entry.revision_key))

    async def get_customer(self, customer_id: str) -> sf.Customer | None:
        self._record("get_customer", customer_id)
        require(CUSTOMER_ID, customer_id, "customer ID")
        for c in self.dataset.customers:
            if c.customer_id == customer_id:
                return sf.Customer(customer_id=c.customer_id, first_name=c.first_name, last_name=c.last_name,
                                   email=c.email, phone=c.phone, city=c.city, preferred_language=c.language,
                                   record=self._ref("Contact", c.customer_id))
        return None

    async def list_demo_customers(self) -> list[sf.CustomerListing]:
        self._record("list_demo_customers")
        return [sf.CustomerListing(customer_id=c.customer_id, first_name=c.first_name, last_name=c.last_name,
                                   preferred_language=c.language) for c in self.dataset.customers]

    async def list_orders(self, customer_id: str) -> list[sf.Order]:
        self._record("list_orders", customer_id)
        require(CUSTOMER_ID, customer_id, "customer ID")
        orders = [o for o in self.dataset.orders if o.customer_id == customer_id]
        orders.sort(key=lambda o: (o.order_date, o.order_number), reverse=True)
        return [self._order(o) for o in orders]

    async def get_order(self, customer_id: str, order_number: str) -> sf.Order | None:
        self._record("get_order", customer_id, order_number)
        require(CUSTOMER_ID, customer_id, "customer ID")
        require(ORDER_NUMBER, order_number, "order number")
        for o in self.dataset.orders:
            if o.order_number == order_number and o.customer_id == customer_id:
                return self._order(o)
        return None

    async def list_cases(self, customer_id: str) -> list[sf.CaseSummary]:
        self._record("list_cases", customer_id)
        return [
            sf.CaseSummary(case_number=f"0000{index:04d}", subject=c.subject, status=c.status, priority=c.priority,
                           category=c.category, order_number=c.order_number,
                           opened_at=datetime.combine(self.dataset.seed_date, time(9), timezone.utc),
                           closed_at=None, is_closed=c.status == "Closed",
                           record=self._ref("Case", c.issue_id))
            for index, c in enumerate(self.dataset.complaints, 1) if c.customer_id == customer_id
        ]

    async def list_published_knowledge(self, entry_type=None) -> list[sf.KnowledgeRevision]:
        self._record("list_published_knowledge", entry_type)
        return [self._knowledge(e) for e in self.dataset.knowledge
                if e.status == "Published" and (entry_type is None or e.entry_type == entry_type)]

    async def get_published_knowledge(self, entry_key: str) -> sf.KnowledgeRevision | None:
        self._record("get_published_knowledge", entry_key)
        require(KNOWLEDGE_KEY, entry_key, "knowledge entry key")
        for e in self.dataset.knowledge:
            if e.entry_key == entry_key and e.status == "Published":
                return self._knowledge(e)
        return None


def unavailable() -> SalesforceUnavailable:
    return SalesforceUnavailable("Salesforce request failed (ConnectError).")


def read_mcp_settings() -> ReadMcpSettings:
    return ReadMcpSettings(context_secret=SECRET, url=MCP_URL, _env_file=None)


def mcp_app(reads: FixtureReads, today: date = fx.SEED_DATE):
    return create_app(read_mcp_settings(), backend=ReadBackend(reads=reads, today=lambda: today))


def token(customer_id: str = "C001", *, ttl: float = 60, **overrides) -> str:
    return context.mint(SECRET, customer_id=customer_id, conversation_id=overrides.pop("conversation_id", "conv-1"),
                        run_id=overrides.pop("run_id", "run-1"), ttl_seconds=ttl, **overrides)


@asynccontextmanager
async def running(app):
    """Run the app's lifespan (the MCP session manager) around a test."""
    async with app.router.lifespan_context(app):
        yield app


def http_client(app, bearer: str | None) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8091",
                              headers=headers)


@asynccontextmanager
async def read_mcp_client(app, bearer: str | None):
    async with http_client(app, bearer) as http:
        async with Client(streamable_http_client(MCP_URL, http_client=http), cache=None) as client:
            yield client


def in_memory_connector(reads: FixtureReads, today: date = fx.SEED_DATE):
    """An MCP connector for the API worker: a fresh in-memory read server per run."""
    from nabd.agents.mcp_tools import http_connector

    @asynccontextmanager
    async def connect(bearer: str):
        app = mcp_app(reads, today)
        async with running(app):
            async with http_connector(MCP_URL, 10, transport=httpx2.ASGITransport(app=app))(bearer) as tools:
                yield tools

    return connect


def tool_json(messages, name):
    """The parsed result of the most recent tool call ``name`` a scripted model saw."""
    import json

    from langchain_core.messages import ToolMessage

    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == name:
            return json.loads(message.content)
    raise AssertionError(f"no {name} result")


def findings(outcome="done", summary="", facts=(), orders=(), questions=()):
    import json

    from fakes import say

    return say(json.dumps({"outcome": outcome, "summary": summary, "facts": list(facts),
                           "order_numbers": list(orders), "questions": list(questions)}))
