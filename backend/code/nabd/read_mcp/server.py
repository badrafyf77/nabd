"""The NABD read MCP server: customer-scoped Salesforce reads for the agents.

Every request needs a bearer context token minted by the API (``nabd.context``).
The SDK's bearer middleware rejects missing or invalid tokens before any tool
runs; tools, resources and the prompt then take the customer only from the
verified token. No tool accepts a customer ID, so a model cannot ask for
another customer's data. Results are public projections: they omit internal
case fields and carry ``sobject:id@version`` evidence references.

Errors reach the client as tool errors whose text starts with a code in square
brackets, for example ``[unavailable]``, so callers can retry or explain them.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from nabd.context import READ_SCOPE, InvalidContext, verify
from nabd.salesforce import reads as sf
from nabd.salesforce.rest import (
    ClientCredentialsAuth, SalesforceAuthError, SalesforceClient, SalesforceError, SalesforceUnavailable,
)
from nabd.salesforce.soql import InvalidReference
from nabd.settings import BusinessSettings, ReadMcpSettings, SalesforceSettings

logger = logging.getLogger(__name__)

SERVER_NAME = "nabd-read"
RETURN_WINDOW_ENTRY = "policy.return_window"
MAX_KNOWLEDGE_PER_CALL = 5
INSTRUCTIONS = (
    "Read-only ShopEasy business data from Salesforce for the signed-in customer only. "
    "Every result carries evidence references (sobject:id@version) to cite in findings. "
    "Knowledge entries are the current published FAQ and policy revisions."
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)


# --- Signed context ------------------------------------------------------------------


class ContextVerifier:
    """Accepts only context tokens signed by the NABD API for this server."""

    def __init__(self, secret: SecretStr):
        self._secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            context = verify(self._secret, token)
        except InvalidContext as error:
            logger.info("Rejected read MCP context: %s", error)
            return None
        return AccessToken(
            token="", client_id="nabd-api", scopes=list(context.scopes), expires_at=context.expires_at,
            subject=context.customer_id,
            claims={"conversation_id": context.conversation_id, "run_id": context.run_id},
        )


def _customer_id() -> str:
    token = get_access_token()
    if token is None or not token.subject or READ_SCOPE not in token.scopes:
        raise ToolError("[unauthorized] This request has no signed customer context.")
    return token.subject


# --- Public projections -----------------------------------------------------------------


def evidence_ref(record: sf.RecordRef) -> str:
    return f"{record.sobject}:{record.id}@{record.version.isoformat().replace('+00:00', 'Z')}"


def day(value: date | None) -> str | None:
    """2026-11-10 -> "2026-11-10 (Tuesday)", so models never work out weekdays."""
    return None if value is None else f"{value.isoformat()} ({value.strftime('%A')})"


class Profile(BaseModel):
    customer_id: str
    first_name: str | None
    last_name: str
    email: str | None
    city: str | None
    preferred_language: str | None
    evidence: list[str]


class DeliveryInfo(BaseModel):
    status: str
    carrier: str | None
    tracking_number: str | None
    shipped: str | None
    expected: str | None
    originally_expected: str | None = Field(description="Set when the delivery date moved.")
    delivered: str | None
    last_location: str | None
    last_update: str | None


class OrderLine(BaseModel):
    line: int
    product: str
    category: str
    quantity: int
    unit_price_gbp: Decimal
    returnable: bool


class OrderSummary(BaseModel):
    order_number: str
    ordered: str
    status: str
    total_gbp: Decimal
    products: list[str]
    delivery: DeliveryInfo | None
    evidence: list[str]


class OrderList(BaseModel):
    orders: list[OrderSummary]
    count: int


class ReturnCheck(BaseModel):
    """Computed in code from the order, its delivery and the published return-window policy."""

    available: bool
    can_return: bool | None
    reason: str
    return_window_days: int | None = None
    return_deadline: str | None = None
    returnable_products: list[str] = []
    non_returnable_products: list[str] = []
    policy_revision: str | None = None
    evidence: list[str] = []


class OrderDetails(BaseModel):
    found: bool
    order_number: str
    message: str | None = None
    ordered: str | None = None
    status: str | None = None
    total_gbp: Decimal | None = None
    payment_method: str | None = None
    items: list[OrderLine] = []
    delivery: DeliveryInfo | None = None
    return_check: ReturnCheck | None = None
    evidence: list[str] = []


class CaseInfo(BaseModel):
    case_number: str
    subject: str | None
    status: str
    category: str | None
    order_number: str | None
    opened: str
    closed: str | None


class CaseList(BaseModel):
    cases: list[CaseInfo]
    count: int
    evidence: list[str]


class KnowledgeIndexEntry(BaseModel):
    key: str
    type: str
    title: str
    question: str | None
    revision: int


class KnowledgeIndex(BaseModel):
    entries: list[KnowledgeIndexEntry]


class KnowledgeArticle(BaseModel):
    key: str
    revision_key: str
    type: str
    title: str
    body: str
    policy_days: int | None
    published: str | None
    evidence: list[str]


class KnowledgeResult(BaseModel):
    entries: list[KnowledgeArticle]
    missing: list[str]


def _delivery(value: sf.Delivery | None) -> DeliveryInfo | None:
    if value is None:
        return None
    moved = value.original_expected_date and value.original_expected_date != value.expected_date
    return DeliveryInfo(
        status=value.status, carrier=value.carrier, tracking_number=value.tracking_number,
        shipped=day(value.shipped_date), expected=day(value.expected_date),
        originally_expected=day(value.original_expected_date) if moved else None,
        delivered=day(value.delivered_date), last_location=value.last_location,
        last_update=day(value.last_update_date),
    )


def _order_evidence(order: sf.Order) -> list[str]:
    refs = [evidence_ref(order.record)]
    if order.delivery:
        refs.append(evidence_ref(order.delivery.record))
    return refs


def return_check(order: sf.Order, policy: sf.KnowledgeRevision | None, today: date) -> ReturnCheck:
    """Whether items of this order can be returned today, decided by code, not the model."""
    if policy is None or policy.policy_days is None:
        return ReturnCheck(available=False, can_return=None,
                           reason="The published return-window policy is not available, so returns "
                                  "cannot be checked right now.")
    days = policy.policy_days
    base = {"return_window_days": days, "policy_revision": policy.revision_key,
            "evidence": [*_order_evidence(order), evidence_ref(policy.record)]}
    blocked = [item.product for item in order.items if not item.returnable]
    allowed = [item.product for item in order.items if item.returnable]
    if order.status == "cancelled":
        return ReturnCheck(available=True, can_return=False, non_returnable_products=blocked,
                           reason="The order was cancelled, so there is nothing to return.", **base)
    delivered = order.delivery.delivered_date if order.delivery else None
    if delivered is None:
        return ReturnCheck(available=True, can_return=False, non_returnable_products=blocked,
                           reason="The order has not been delivered yet.", **base)
    deadline = delivered + timedelta(days=days)
    if today > deadline:
        return ReturnCheck(
            available=True, can_return=False, return_deadline=day(deadline), non_returnable_products=blocked,
            reason=f"The {days}-day return window ended on {day(deadline)}.", **base)
    if not allowed:
        return ReturnCheck(
            available=True, can_return=False, return_deadline=day(deadline), non_returnable_products=blocked,
            reason="None of the items in this order can be returned.", **base)
    return ReturnCheck(
        available=True, can_return=True, return_deadline=day(deadline), returnable_products=allowed,
        non_returnable_products=blocked,
        reason=f"Returnable items can be sent back until {day(deadline)}.", **base)


def _article(entry: sf.KnowledgeRevision) -> KnowledgeArticle:
    return KnowledgeArticle(
        key=entry.entry_key, revision_key=entry.revision_key, type=entry.entry_type, title=entry.title,
        body=entry.body, policy_days=entry.policy_days,
        published=entry.published_at.date().isoformat() if entry.published_at else None,
        evidence=[evidence_ref(entry.record)],
    )


# --- The server ------------------------------------------------------------------------


@dataclass
class ReadBackend:
    """What the tools read through; filled by the app lifespan (or a test)."""

    reads: sf.SalesforceReads | None = None
    today: Callable[[], date] = field(default=date.today)

    def require(self) -> sf.SalesforceReads:
        if self.reads is None:
            raise ToolError("[unavailable] The Salesforce connection is not ready.")
        return self.reads


async def _call(operation):
    """Run one adapter call, turning failures into coded, secret-free tool errors."""
    try:
        return await operation
    except InvalidReference as error:
        raise ToolError(f"[invalid_argument] {error}.") from None
    except SalesforceUnavailable:
        raise ToolError("[unavailable] Salesforce is temporarily unavailable. Try again shortly.") from None
    except SalesforceAuthError:
        raise ToolError("[unavailable] The Salesforce integration could not sign in.") from None
    except SalesforceError:
        raise ToolError("[failed] Salesforce refused the request.") from None


def build_server(backend: ReadBackend, secret: SecretStr, *, issuer_url: str, resource_url: str) -> MCPServer:
    server = MCPServer(
        SERVER_NAME,
        title="NABD read MCP",
        version="0.2.0",
        instructions=INSTRUCTIONS,
        token_verifier=ContextVerifier(secret),
        auth=AuthSettings(issuer_url=issuer_url, resource_server_url=resource_url,
                          required_scopes=[READ_SCOPE], validate_token_resource=False),
        subscriptions=False,
    )

    async def profile() -> Profile:
        customer_id = _customer_id()
        customer = await _call(backend.require().get_customer(customer_id))
        if customer is None:
            raise ToolError("[not_found] The signed-in customer was not found in Salesforce.")
        return Profile(
            customer_id=customer.customer_id, first_name=customer.first_name, last_name=customer.last_name,
            email=customer.email, city=customer.city, preferred_language=customer.preferred_language,
            evidence=[evidence_ref(customer.record)],
        )

    async def index() -> KnowledgeIndex:
        _customer_id()
        entries = await _call(backend.require().list_published_knowledge())
        return KnowledgeIndex(entries=[
            KnowledgeIndexEntry(key=e.entry_key, type=e.entry_type, title=e.title, question=e.question,
                                revision=e.revision)
            for e in sorted(entries, key=lambda e: e.entry_key)
        ])

    @server.tool(title="Customer profile", annotations=READ_ONLY)
    async def get_customer_profile() -> Profile:
        """The signed-in customer's name, email, city and preferred language."""
        return await profile()

    @server.tool(title="List orders", annotations=READ_ONLY)
    async def list_orders() -> OrderList:
        """The signed-in customer's recent orders, newest first: products, total and delivery status/dates."""
        orders = await _call(backend.require().list_orders(_customer_id()))
        return OrderList(count=len(orders), orders=[
            OrderSummary(
                order_number=o.order_number, ordered=day(o.order_date), status=o.status, total_gbp=o.total_gbp,
                products=[item.product for item in o.items], delivery=_delivery(o.delivery),
                evidence=_order_evidence(o),
            )
            for o in orders
        ])

    @server.tool(title="Order details", annotations=READ_ONLY)
    async def get_order(order_number: str = Field(description="Order number such as SE-1042.")) -> OrderDetails:
        """One of the customer's orders with items, delivery and a computed return check.

        An order that is not on this customer's account is reported as not found.
        """
        customer_id = _customer_id()
        reads = backend.require()
        normalized = order_number.strip().upper()
        order = await _call(reads.get_order(customer_id, normalized))
        if order is None:
            return OrderDetails(found=False, order_number=normalized,
                                message="This order is not on the signed-in customer's account.")
        policy = await _call(reads.get_published_knowledge(RETURN_WINDOW_ENTRY))
        return OrderDetails(
            found=True, order_number=order.order_number, ordered=day(order.order_date), status=order.status,
            total_gbp=order.total_gbp, payment_method=order.payment_method,
            items=[OrderLine(line=i.line_number, product=i.product, category=i.category, quantity=i.quantity,
                             unit_price_gbp=i.unit_price_gbp, returnable=i.returnable) for i in order.items],
            delivery=_delivery(order.delivery), return_check=return_check(order, policy, backend.today()),
            evidence=[*_order_evidence(order), *(evidence_ref(i.record) for i in order.items)],
        )

    @server.tool(title="List cases", annotations=READ_ONLY)
    async def list_cases() -> CaseList:
        """The customer's support cases (complaints): number, subject, status, category and order."""
        cases = await _call(backend.require().list_cases(_customer_id()))
        return CaseList(count=len(cases), evidence=[evidence_ref(c.record) for c in cases], cases=[
            CaseInfo(case_number=c.case_number, subject=c.subject, status=c.status, category=c.category,
                     order_number=c.order_number, opened=day(c.opened_at.date()),
                     closed=day(c.closed_at.date()) if c.closed_at else None)
            for c in cases
        ])

    @server.tool(title="Knowledge index", annotations=READ_ONLY)
    async def list_knowledge() -> KnowledgeIndex:
        """Index of the current published ShopEasy FAQ and policy entries (key, title, question)."""
        return await index()

    @server.tool(title="Knowledge entries", annotations=READ_ONLY)
    async def get_knowledge(
        entry_keys: list[str] = Field(description="Up to five keys from the knowledge index, "
                                                  "such as faq.delivery_cost or policy.refunds."),
    ) -> KnowledgeResult:
        """Full text of published FAQ/policy entries, with their revision for citation."""
        _customer_id()
        keys = list(dict.fromkeys(key.strip() for key in entry_keys))[:MAX_KNOWLEDGE_PER_CALL]
        found, missing = [], []
        for key in keys:
            entry = await _call(backend.require().get_published_knowledge(key))
            (found.append(_article(entry)) if entry else missing.append(key))
        return KnowledgeResult(entries=found, missing=missing)

    @server.resource("nabd://customer/profile", name="customer_profile", mime_type="application/json",
                     description="The signed-in customer's profile.")
    async def profile_resource() -> str:
        try:
            return (await profile()).model_dump_json()
        except ToolError as error:
            raise ResourceError(str(error)) from None

    @server.resource("nabd://knowledge/index", name="knowledge_index", mime_type="application/json",
                     description="Current published FAQ and policy entries.")
    async def knowledge_index_resource() -> str:
        try:
            return (await index()).model_dump_json()
        except ToolError as error:
            raise ResourceError(str(error)) from None

    @server.resource("nabd://knowledge/{entry_key}", name="knowledge_entry", mime_type="application/json",
                     description="One current published FAQ or policy entry.")
    async def knowledge_entry_resource(entry_key: str) -> str:
        try:
            _customer_id()
            entry = await _call(backend.require().get_published_knowledge(entry_key))
        except ToolError as error:
            raise ResourceError(str(error)) from None
        if entry is None:
            raise ResourceError(f"[not_found] No published entry {entry_key}.")
        return _article(entry).model_dump_json()

    @server.prompt(title="Staff review of a customer's support context")
    async def staff_review(focus: str = "the customer's open questions") -> str:
        """A customer-scoped review brief: orders, cases and published policies to check answers against."""
        customer = await profile()
        orders = await _call(backend.require().list_orders(customer.customer_id))
        cases = await _call(backend.require().list_cases(customer.customer_id))
        knowledge = await index()
        lines = [
            f"Review the support context of ShopEasy customer {customer.customer_id} "
            f"({customer.first_name or ''} {customer.last_name}). Focus: {focus}.",
            "Check every statement made to the customer against these Salesforce records and cite the "
            "evidence reference or policy revision for each conclusion. Separate verified facts from what "
            "the customer reported, and list anything that needs staff action.",
            "",
            "Orders:",
            *[f"- {o.order_number} ({o.status}, {day(o.order_date)}): "
              f"{', '.join(i.product for i in o.items) or 'no items'}; delivery "
              f"{o.delivery.status if o.delivery else 'none'} [{', '.join(_order_evidence(o))}]" for o in orders],
            "",
            "Cases:",
            *([f"- {c.case_number} {c.status} {c.category or 'uncategorised'}: {c.subject or ''} "
               f"[{evidence_ref(c.record)}]" for c in cases] or ["- none"]),
            "",
            "Published policies and FAQs (revision):",
            *[f"- {e.key}@{e.revision}: {e.title}" for e in knowledge.entries],
        ]
        return "\n".join(lines)

    @server.custom_route("/healthz", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"service": SERVER_NAME, "status": "ok"})

    return server


def create_app(
    settings: ReadMcpSettings | None = None,
    salesforce: SalesforceSettings | None = None,
    business: BusinessSettings | None = None,
    *,
    backend: ReadBackend | None = None,
) -> Starlette:
    """The ASGI app. With ``backend`` given (tests), no Salesforce connection is opened."""
    config = settings or ReadMcpSettings()
    calendar = business or BusinessSettings()
    supplied = backend is not None
    backend = backend or ReadBackend(today=calendar.today)
    server = build_server(backend, config.context_secret, issuer_url="http://127.0.0.1:8081",
                          resource_url=config.url)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, host=config.host,
        transport_security=TransportSecuritySettings(allowed_hosts=list(config.allowed_host_list)),
    )
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette):
        if supplied:
            async with inner(application):
                yield
            return
        sf_settings = salesforce or SalesforceSettings()
        async with httpx.AsyncClient(timeout=sf_settings.timeout_seconds) as http:
            auth = ClientCredentialsAuth(sf_settings.domain_url, sf_settings.client_id,
                                         sf_settings.client_secret, http)
            backend.reads = sf.SalesforceReads(SalesforceClient(auth, http, sf_settings.api_version))
            async with inner(application):
                yield
            backend.reads = None

    app.router.lifespan_context = lifespan
    return app
