"""Test doubles for the NABD chat runtime. No network, no cost.

``FixtureReads`` answers like ``SalesforceReads`` but from the Phase 1 fixture
dataset, so MCP and agent tests run against realistic ShopEasy records.
``read_mcp_client`` connects a real MCP client to the real read server over
Streamable HTTP, carried in memory by an ASGI transport.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from nabd import context
from nabd.read_mcp.server import ReadBackend, create_app
from nabd.runtime.store import CommandRow, NewCommand
from nabd.salesforce import fixtures as fx
from nabd.salesforce import reads as sf
from nabd.salesforce import schema as s
from nabd.salesforce.commands import CaseCommand, CommandResult, salesforce_time
from nabd.salesforce.rest import SalesforceUnavailable
from nabd.salesforce.seed import summary_hash
from nabd.salesforce.soql import CUSTOMER_ID, KNOWLEDGE_KEY, ORDER_NUMBER, require
from nabd.settings import ReadMcpSettings, WriteMcpSettings

SECRET = SecretStr("test-context-secret-that-is-long-enough")
WRITE_SECRET = SecretStr("test-write-context-secret-long-enough")
MCP_URL = "http://127.0.0.1:8091/mcp"
WRITE_URL = "http://127.0.0.1:8092/mcp"
STAMP = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
RANK = {"Low": 1, "Medium": 2, "High": 3}


@dataclass
class FakeCase:
    """A Salesforce case as the fake org holds it."""

    id: str
    number: str
    customer_id: str
    issue_id: str | None
    subject: str
    status: str
    category: str | None
    order_number: str | None
    priority: str | None
    assessed_priority: str | None
    summary: str | None
    summary_hash: str | None
    sentiment: str | None = None
    urgency: str | None = None
    needs_review: bool = False
    staff_action: str | None = None
    staff_action_completed: bool = False
    reopen_count: int = 0
    resolution: str | None = None
    confirmation: str | None = None
    queue: str | None = None
    version: datetime = STAMP
    created: datetime = STAMP
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def closed(self) -> bool:
        return self.status == "Closed"


class FixtureReads:
    """``SalesforceReads`` stand-in over ``fixtures.build()``; records every call."""

    def __init__(self, dataset: fx.Dataset | None = None):
        self.dataset = dataset or fx.build()
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next: list[Exception] = []
        self.fail_on: dict[str, list[Exception]] = {}  # adapter method name -> errors to raise next
        self._ids = itertools.count(1)
        self._refs: dict[tuple[str, str], sf.RecordRef] = {}
        self._clock = itertools.count(1)
        self.cases: dict[str, FakeCase] = {}
        for index, c in enumerate(self.dataset.complaints, 1):
            case = FakeCase(
                id=f"500FX{index:010d}AAA", number=f"{1000 + index:08d}", customer_id=c.customer_id,
                issue_id=c.issue_id, subject=c.subject, status=c.status, category=c.category,
                order_number=c.order_number, priority=c.priority, assessed_priority=c.priority, summary=c.summary,
                summary_hash=summary_hash(c.summary), sentiment=c.sentiment, urgency=c.urgency,
                needs_review=c.needs_review, staff_action=c.staff_action,
                staff_action_completed=c.staff_action_completed, resolution=c.outcome,
                queue=s.QUEUE_FOR_CATEGORY[c.category].developer_name)
            self.cases[case.id] = case

    def touch(self, case: FakeCase) -> None:
        """A save: Salesforce moves SystemModstamp forward."""
        case.version = STAMP + timedelta(minutes=next(self._clock))

    def staff_edit(self, case_number: str, **changes) -> FakeCase:
        """Simulate a staff member changing a case in Salesforce."""
        case = self.case(case_number)
        for name, value in changes.items():
            setattr(case, name, value)
        self.touch(case)
        return case

    def case(self, case_number: str) -> FakeCase:
        return next(c for c in self.cases.values() if c.number == case_number)

    def customer_cases(self, customer_id: str) -> list[FakeCase]:
        return sorted((c for c in self.cases.values() if c.customer_id == customer_id),
                      key=lambda c: (c.created, c.number), reverse=True)

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
            sf.CaseSummary(case_number=c.number, subject=c.subject, status=c.status, priority=c.priority,
                           category=c.category, order_number=c.order_number, opened_at=c.created,
                           closed_at=c.version if c.closed else None, is_closed=c.closed,
                           record=sf.RecordRef(sobject="Case", id=c.id, version=c.version))
            for c in self.customer_cases(customer_id)
        ]

    async def list_case_history(self, customer_id: str) -> list[sf.CaseHistoryEntry]:
        self._record("list_case_history", customer_id)
        require(CUSTOMER_ID, customer_id, "customer ID")
        return [
            sf.CaseHistoryEntry(
                case_number=c.number, subject=c.subject, status=c.status, category=c.category,
                order_number=c.order_number, opened_at=c.created, closed_at=c.version if c.closed else None,
                is_closed=c.closed, summary=c.summary, reopen_count=c.reopen_count, last_reopened_at=None,
                resolution=c.resolution, staff_action=c.staff_action,
                staff_action_completed=c.staff_action_completed,
                record=sf.RecordRef(sobject="Case", id=c.id, version=c.version))
            for c in self.customer_cases(customer_id)
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


class FakeGateway:
    """``SalesforceCommands`` stand-in that follows the Apex gateway's rules on ``FixtureReads`` cases.

    ``fail`` injects outcomes for the next calls: "before" raises before anything
    happens, "after" commits and then raises (the caller never hears the answer).
    """

    def __init__(self, reads: FixtureReads):
        self.reads = reads
        self.receipts: dict[str, tuple[str, CommandResult, str]] = {}
        self.calls: list[CaseCommand] = []
        self.fail: list[str] = []
        self._numbers = itertools.count(2001)

    async def execute(self, command: CaseCommand) -> CommandResult:
        self.calls.append(command)
        mode = self.fail.pop(0) if self.fail else None
        if mode == "before":
            raise unavailable()
        result = self._execute(command)
        if mode == "after":
            raise unavailable()
        return result

    async def receipt(self, operation_id: str, customer_id: str) -> CommandResult:
        stored = self.receipts.get(operation_id)
        if stored is None or stored[2] != customer_id:
            return CommandResult(status="unknown", operation_id=operation_id)
        return stored[1].model_copy(update={"replayed": True})

    def _execute(self, c: CaseCommand) -> CommandResult:
        fingerprint = hashlib.sha256(c.body()).hexdigest()
        if c.operation_id in self.receipts:
            stored = self.receipts[c.operation_id]
            if stored[0] != fingerprint:
                return self._result(c, "failed", "operation_reused")
            return stored[1].model_copy(update={"replayed": True})
        kinds = {e.kind for e in c.evidence}
        if c.operation == "create_case" and "customer_report" not in kinds:
            return self._result(c, "failed", "invalid_command")
        if c.operation in ("request_closure", "close_case") and "verified_fact" not in kinds:
            return self._result(c, "failed", "invalid_command")
        if c.operation == "close_case" and not any(e.kind == "customer_confirmation" and e.ref == c.confirmation_message_id
                                                   for e in c.evidence):
            return self._result(c, "failed", "invalid_command")
        result = self._apply(c).model_copy(update={"recorded": True})
        self.receipts[c.operation_id] = (fingerprint, result, c.customer_id)
        return result

    def _result(self, c: CaseCommand, status: str, reason: str | None = None,
                case: FakeCase | None = None, **extra) -> CommandResult:
        values = {"status": status, "reason": reason, "operation_id": c.operation_id, "operation": c.operation, **extra}
        if case is not None:
            values.update(case_id=case.id, case_number=case.number, case_version=salesforce_time(case.version),
                          case_status=case.status, queue=case.queue)
        return CommandResult(**values)

    def _apply(self, c: CaseCommand) -> CommandResult:
        if not any(customer.customer_id == c.customer_id for customer in self.reads.dataset.customers):
            return self._result(c, "failed", "unknown_customer")
        if c.operation == "create_case":
            same = next((x for x in self.reads.cases.values() if x.issue_id == c.issue_id), None)
            if same is not None:
                return self._result(c, "conflict", "issue_exists", same)
            if c.order_number and not any(o.order_number == c.order_number and o.customer_id == c.customer_id
                                          for o in self.reads.dataset.orders):
                return self._result(c, "failed", "order_not_found")
            number = next(self._numbers)
            case = FakeCase(
                id=f"500NB{number:010d}AAA", number=f"{number:08d}", customer_id=c.customer_id, issue_id=c.issue_id,
                subject=c.subject or "", status="New", category=c.category, order_number=c.order_number,
                priority=c.priority, assessed_priority=c.priority, summary=c.summary,
                summary_hash=summary_hash(c.summary or ""), sentiment=c.sentiment, urgency=c.urgency,
                needs_review=bool(c.needs_review), staff_action=c.staff_action,
                queue=s.QUEUE_FOR_CATEGORY[c.category].developer_name)
            case.created = STAMP + timedelta(minutes=next(self.reads._clock))
            case.version = case.created
            self.reads.cases[case.id] = case
            added = self._evidence(case, c)
            return self._result(c, "confirmed", None, case, evidence_added=added)
        case = self.reads.cases.get(c.case_id or "")
        if case is None or case.customer_id != c.customer_id:
            return self._result(c, "failed", "case_not_found")
        if salesforce_time(case.version) != c.expected_version:
            return self._result(c, "conflict", "version_changed", case)
        overrides: list[str] = []
        if c.operation == "reopen_case":
            if not case.closed:
                return self._result(c, "conflict", "case_open", case)
            if case.resolution == "duplicate":
                return self._result(c, "conflict", "closed_as_duplicate", case)
            case.status, case.reopen_count, case.resolution, case.confirmation = "Working", case.reopen_count + 1, None, None
            if case.staff_action:
                case.staff_action_completed = False
        elif case.closed:
            return self._result(c, "conflict", "case_closed", case)
        elif c.operation == "update_case":
            if case.status == "Waiting on Customer":
                case.status = "Working"
        elif case.staff_action and not case.staff_action_completed:
            return self._result(c, "conflict", "staff_action_pending", case)
        elif c.operation == "request_closure":
            case.status = "Waiting on Customer"
        else:
            case.status, case.resolution, case.confirmation = "Closed", "resolved", c.confirmation_message_id
        if c.sentiment:
            case.sentiment = c.sentiment
        if c.urgency:
            case.urgency = c.urgency
        if c.priority and RANK[c.priority] > RANK.get(case.priority or "", 0):
            if case.assessed_priority and case.assessed_priority == case.priority:
                case.priority = case.assessed_priority = c.priority
            else:
                overrides.append("priority")
        if c.summary and c.summary != case.summary:
            if case.summary_hash and summary_hash(case.summary or "") == case.summary_hash:
                case.summary, case.summary_hash = c.summary, summary_hash(c.summary)
            else:
                overrides.append("summary")
        if c.staff_action and not case.staff_action:
            case.staff_action, case.staff_action_completed = c.staff_action, False
        self.reads.touch(case)
        added = self._evidence(case, c)
        return self._result(c, "confirmed", None, case, overrides=tuple(overrides), evidence_added=added)

    @staticmethod
    def _evidence(case: FakeCase, c: CaseCommand) -> int:
        prefix = case.issue_id or f"case:{case.id}"
        added = 0
        for item in c.evidence:
            key = f"{prefix}:{item.ref}"
            if key not in case.evidence:
                case.evidence[key] = item.model_dump()
                added += 1
        return added


class MemoryCaseStore:
    """The case-work part of ``Store`` in memory, for agent tests without PostgreSQL."""

    def __init__(self):
        self.plans: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, CommandRow] = {}
        self.links: dict[tuple[str, str], dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.waits = 0

    @asynccontextmanager
    async def case_lock(self, customer_id: str, on_wait=None):
        lock = self._locks[customer_id]
        waited = lock.locked()
        if waited:
            self.waits += 1
            if on_wait is not None:
                await on_wait()
        async with lock:
            yield waited

    async def case_plan(self, run_id: str):
        rows = [row for row in self.commands.values() if row.run_id == run_id]
        return self.plans.get(run_id), rows

    async def save_case_plan(self, run_id, conversation_id, customer_id, assessment, commands) -> bool:
        if run_id in self.plans:
            return False
        self.plans[run_id] = json.loads(json.dumps(assessment, default=str))
        for command in commands:
            await self.add_command(run_id, conversation_id, customer_id, command)
        return True

    async def add_command(self, run_id, conversation_id, customer_id, command: NewCommand) -> None:
        self.commands.setdefault(command.operation_id, CommandRow(
            operation_id=command.operation_id, run_id=run_id, conversation_id=conversation_id,
            customer_id=customer_id, position=command.position, issue_ref=command.issue_ref,
            operation=command.operation, request=json.loads(json.dumps(command.request)), card=dict(command.card),
            status="pending", attempts=0, result=None, error_code=None))

    async def mark_command(self, operation_id, status, *, result=None, error=None, sent=False) -> None:
        row = self.commands[operation_id]
        self.commands[operation_id] = dataclasses.replace(
            row, status=status, result=result if result is not None else row.result, error_code=error,
            attempts=row.attempts + (1 if sent else 0))

    async def record_case(self, conversation_id, customer_id, run_id, *, case_id, case_number, department, status,
                          operation) -> None:
        self.links[(conversation_id, case_id)] = {"case_number": case_number, "department": department,
                                                   "status": status, "last_operation": operation}


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


def write_settings() -> WriteMcpSettings:
    return WriteMcpSettings(context_secret=WRITE_SECRET, url=WRITE_URL, _env_file=None)


def write_app(gateway: FakeGateway):
    from nabd.write_mcp.server import WriteBackend
    from nabd.write_mcp.server import create_app as create_write_app

    return create_write_app(write_settings(), backend=WriteBackend(commands=gateway))


def write_token(customer_id: str = "C001", *, conversation_id: str = "conv-1", run_id: str = "run-1",
                ttl: float = 60) -> str:
    return context.mint(WRITE_SECRET, customer_id=customer_id, conversation_id=conversation_id, run_id=run_id,
                        ttl_seconds=ttl, scopes=(context.WRITE_SCOPE,), audience=context.WRITE_AUDIENCE)


def case_token(customer_id: str = "C001", **overrides) -> str:
    return token(customer_id, scopes=(context.READ_SCOPE, context.CASE_HISTORY_SCOPE), **overrides)


def in_memory_write_connector(gateway: FakeGateway):
    """A write MCP connector over an in-memory write server and the fake gateway."""
    from nabd.agents.mcp_tools import http_connector

    @asynccontextmanager
    async def connect(bearer: str):
        app = write_app(gateway)
        async with running(app):
            async with http_connector(WRITE_URL, 10, transport=httpx2.ASGITransport(app=app))(bearer) as tools:
                yield tools

    return connect


def assessment(*issues: dict[str, Any], outcome: str | None = None, question: str | None = None):
    """A scripted Complaint specialist answer."""
    from fakes import say

    return say(json.dumps({"outcome": outcome or ("issues" if issues else "no_complaint"), "question": question,
                           "issues": list(issues)}))


def issue(**values: Any) -> dict[str, Any]:
    """One scripted issue with sensible defaults; override what the test is about."""
    base = {"title": "Problem", "existing_case": None, "match": "new", "action": "create",
            "category": "Delivery", "category_confidence": 0.9, "sentiment": "negative", "urgency": "medium",
            "urgency_reason": "The customer is waiting.", "priority_signals": [], "staff_action": "none",
            "language": "en", "order_numbers": [], "customer_message_ids": [], "reported": [],
            "verified_facts": [], "english_summary": "Customer reports a problem.",
            "confirmation_message_id": None, "question": None}
    return {**base, **values}


def no_complaint_model():
    from fakes import FakeChatModel

    return FakeChatModel(default=assessment())


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
