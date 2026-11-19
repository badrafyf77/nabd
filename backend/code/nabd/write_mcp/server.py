"""The NABD write MCP server: named case operations for the signed-in customer.

Every request needs a bearer context token minted by the API for this server
(audience ``nabd-write-mcp``, scope ``nabd.write.cases``, its own secret). The
customer and conversation come only from that token; no tool takes a customer
ID. Each tool builds one typed ``CaseCommand`` and sends it to the Salesforce
case command gateway, which enforces ownership, versions, transitions, staff
overrides and receipts in one transaction.

Agents never see these tools. NABD's validated case workflow calls them after
code has checked a Complaint specialist's proposal.

Errors reach the client as tool errors starting with a code in square
brackets. ``[unavailable]`` means Salesforce did not answer: the operation may
or may not have happened, so look it up with ``get_operation`` before retrying.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field, SecretStr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from nabd.context import WRITE_AUDIENCE, WRITE_SCOPE, InvalidContext, verify
from nabd.salesforce.commands import (
    OPERATION_ID_PATTERN, CaseCommand, Category, CommandResult, Evidence, Language, Priority,
    SalesforceCommands, Sentiment, StaffAction, Urgency,
)
from nabd.salesforce.rest import (
    ClientCredentialsAuth, SalesforceAuthError, SalesforceClient, SalesforceError, SalesforceUnavailable,
)
from nabd.settings import SalesforceSettings, WriteMcpSettings

logger = logging.getLogger(__name__)

SERVER_NAME = "nabd-write"
INSTRUCTIONS = (
    "Named ShopEasy case operations for the signed-in customer, executed by the Salesforce case command "
    "gateway. Every operation needs a durable operation_id: repeating the same request returns the stored "
    "result instead of acting twice. After an [unavailable] error, call get_operation before retrying."
)
CASE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                             open_world_hint=False)
LOOKUP = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

OperationId = Annotated[str, Field(pattern=OPERATION_ID_PATTERN,
                                   description="Durable ID of this side effect; reuse it only to retry it.")]
CaseId = Annotated[str, Field(pattern=r"^500[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$", description="Salesforce case ID.")]
Version = Annotated[str, Field(min_length=20, max_length=40,
                               description="The case SystemModstamp NABD read, such as 2026-11-18T09:00:00.000Z.")]
EvidenceList = Annotated[list[Evidence], Field(max_length=20)]


class ContextVerifier:
    """Accepts only context tokens the NABD API signed for the write server."""

    def __init__(self, secret: SecretStr):
        self._secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            context = verify(self._secret, token, audience=WRITE_AUDIENCE)
        except InvalidContext as error:
            logger.info("Rejected write MCP context: %s", error)
            return None
        return AccessToken(
            token="", client_id="nabd-api", scopes=list(context.scopes), expires_at=context.expires_at,
            subject=context.customer_id,
            claims={"conversation_id": context.conversation_id, "run_id": context.run_id},
        )


def _signed() -> tuple[str, str]:
    """(customer ID, conversation ID) from the verified token."""
    token = get_access_token()
    if token is None or not token.subject or WRITE_SCOPE not in token.scopes:
        raise ToolError("[unauthorized] This request has no signed customer context for case operations.")
    return token.subject, str((token.claims or {}).get("conversation_id") or "")[:64]


@dataclass
class WriteBackend:
    """What the tools send commands through; filled by the app lifespan (or a test)."""

    commands: SalesforceCommands | None = None

    def require(self) -> SalesforceCommands:
        if self.commands is None:
            raise ToolError("[unavailable] The Salesforce connection is not ready.")
        return self.commands


def build_server(backend: WriteBackend, secret: SecretStr, *, issuer_url: str, resource_url: str) -> MCPServer:
    server = MCPServer(
        SERVER_NAME,
        title="NABD write MCP",
        version="0.3.0",
        instructions=INSTRUCTIONS,
        token_verifier=ContextVerifier(secret),
        auth=AuthSettings(issuer_url=issuer_url, resource_server_url=resource_url,
                          required_scopes=[WRITE_SCOPE], validate_token_resource=False),
        subscriptions=False,
    )

    async def send(operation: str, **values) -> CommandResult:
        customer_id, conversation_id = _signed()
        try:
            command = CaseCommand(operation=operation, customer_id=customer_id, conversation_id=conversation_id,
                                  **values)
        except ValidationError as error:
            raise ToolError(f"[invalid_argument] {error.error_count()} invalid argument(s).") from None
        try:
            return await backend.require().execute(command)
        except SalesforceUnavailable:
            raise ToolError("[unavailable] Salesforce did not answer; the operation may or may not have "
                            "happened. Look it up with get_operation before retrying.") from None
        except SalesforceAuthError:
            raise ToolError("[unavailable] The Salesforce integration could not sign in.") from None
        except SalesforceError:
            raise ToolError("[failed] Salesforce refused the request.") from None

    @server.tool(title="Create case", annotations=CASE_WRITE)
    async def create_case(
        operation_id: OperationId,
        issue_id: Annotated[str, Field(pattern=OPERATION_ID_PATTERN, description="Stable ID of the distinct issue.")],
        category: Category,
        subject: Annotated[str, Field(min_length=1, max_length=255)],
        summary: Annotated[str, Field(min_length=1, max_length=32000, description="English summary for staff.")],
        priority: Priority,
        priority_reason: Annotated[str, Field(max_length=255)],
        sentiment: Sentiment,
        urgency: Urgency,
        confidence: Annotated[float, Field(ge=0, le=1, description="Category confidence.")],
        needs_review: bool,
        evidence: EvidenceList,
        language: Language = "en",
        order_number: Annotated[str | None, Field(pattern=r"^SE-\d{4,6}$")] = None,
        staff_action: StaffAction | None = None,
    ) -> CommandResult:
        """Open a case for a new distinct complaint, routed to its category's team queue."""
        return await send("create_case", operation_id=operation_id, issue_id=issue_id, category=category,
                          subject=subject, summary=summary, priority=priority, priority_reason=priority_reason,
                          sentiment=sentiment, urgency=urgency, confidence=confidence, needs_review=needs_review,
                          evidence=tuple(evidence), language=language, order_number=order_number,
                          staff_action=staff_action)

    @server.tool(title="Update case", annotations=CASE_WRITE)
    async def update_case(
        operation_id: OperationId, case_id: CaseId, expected_version: Version, evidence: EvidenceList,
        summary: Annotated[str | None, Field(max_length=32000)] = None, sentiment: Sentiment | None = None,
        urgency: Urgency | None = None, priority: Priority | None = None,
        priority_reason: Annotated[str | None, Field(max_length=255)] = None,
        staff_action: StaffAction | None = None,
    ) -> CommandResult:
        """Add the customer's new information to their open case for the same issue."""
        return await send("update_case", operation_id=operation_id, case_id=case_id,
                          expected_version=expected_version, evidence=tuple(evidence), summary=summary,
                          sentiment=sentiment, urgency=urgency, priority=priority, priority_reason=priority_reason,
                          staff_action=staff_action)

    @server.tool(title="Reopen case", annotations=CASE_WRITE)
    async def reopen_case(
        operation_id: OperationId, case_id: CaseId, expected_version: Version, evidence: EvidenceList,
        summary: Annotated[str | None, Field(max_length=32000)] = None, sentiment: Sentiment | None = None,
        urgency: Urgency | None = None, priority: Priority | None = None,
        priority_reason: Annotated[str | None, Field(max_length=255)] = None,
        staff_action: StaffAction | None = None,
    ) -> CommandResult:
        """Reopen the customer's closed case because the same issue came back."""
        return await send("reopen_case", operation_id=operation_id, case_id=case_id,
                          expected_version=expected_version, evidence=tuple(evidence), summary=summary,
                          sentiment=sentiment, urgency=urgency, priority=priority, priority_reason=priority_reason,
                          staff_action=staff_action)

    @server.tool(title="Request case closure", annotations=CASE_WRITE)
    async def request_case_closure(
        operation_id: OperationId, case_id: CaseId, expected_version: Version, evidence: EvidenceList,
        sentiment: Sentiment | None = None,
    ) -> CommandResult:
        """Record verified resolution evidence and wait for the customer to confirm the issue is solved."""
        return await send("request_closure", operation_id=operation_id, case_id=case_id,
                          expected_version=expected_version, evidence=tuple(evidence), sentiment=sentiment)

    @server.tool(title="Close case", annotations=CASE_WRITE)
    async def close_case(
        operation_id: OperationId, case_id: CaseId, expected_version: Version,
        confirmation_message_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]{1,70}$")],
        evidence: EvidenceList, sentiment: Sentiment | None = None,
    ) -> CommandResult:
        """Close a case with verified resolution evidence and the customer's confirmation message."""
        return await send("close_case", operation_id=operation_id, case_id=case_id,
                          expected_version=expected_version, confirmation_message_id=confirmation_message_id,
                          evidence=tuple(evidence), sentiment=sentiment)

    @server.tool(title="Revise case summary", annotations=CASE_WRITE)
    async def revise_case_summary(
        operation_id: OperationId, case_id: CaseId, expected_version: Version,
        summary: Annotated[str, Field(min_length=1, max_length=32000)],
    ) -> CommandResult:
        """Correct NABD-owned English summary text without changing the case lifecycle."""
        return await send("revise_summary", operation_id=operation_id, case_id=case_id,
                          expected_version=expected_version, summary=summary)

    @server.tool(title="Operation receipt", annotations=LOOKUP)
    async def get_operation(operation_id: OperationId) -> CommandResult:
        """The stored outcome of an earlier operation, or status unknown if it never happened."""
        customer_id, _ = _signed()
        try:
            return await backend.require().receipt(operation_id, customer_id)
        except (SalesforceUnavailable, SalesforceAuthError):
            raise ToolError("[unavailable] Salesforce did not answer the receipt lookup.") from None
        except SalesforceError:
            raise ToolError("[failed] Salesforce refused the receipt lookup.") from None

    @server.custom_route("/healthz", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"service": SERVER_NAME, "status": "ok"})

    return server


def create_app(
    settings: WriteMcpSettings | None = None,
    salesforce: SalesforceSettings | None = None,
    *,
    backend: WriteBackend | None = None,
) -> Starlette:
    """The ASGI app. With ``backend`` given (tests), no Salesforce connection is opened."""
    config = settings or WriteMcpSettings()
    supplied = backend is not None
    backend = backend or WriteBackend()
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
            backend.commands = SalesforceCommands(SalesforceClient(auth, http, sf_settings.api_version))
            async with inner(application):
                yield
            backend.commands = None

    app.router.lifespan_context = lifespan
    return app
