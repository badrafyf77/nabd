"""Typed client of the Salesforce case command gateway (Apex ``NabdCaseCommands``).

A ``CaseCommand`` is one named business operation with schema-checked
arguments. Its JSON body is canonical (sorted keys, no spaces), so a retry of
the same stored command sends byte-identical bytes: the gateway hashes the body
and returns the stored receipt instead of acting twice.

Outcomes:

* ``CommandResult`` with status ``confirmed``, ``conflict`` or ``failed`` is
  definite: Salesforce decided, with or without a saved receipt;
* ``SalesforceUnavailable`` (timeout, network, 5xx) is **ambiguous**: the
  command may or may not have committed. Look up the receipt before retrying.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nabd.salesforce import schema as s
from nabd.salesforce.rest import SalesforceClient

GATEWAY_PATH = "/services/apexrest/nabd/v1/case-commands"

Operation = Literal["create_case", "update_case", "reopen_case", "request_closure", "close_case", "revise_summary"]
Category = Literal[tuple(value.api for value in s.COMPLAINT_CATEGORIES)]  # type: ignore[valid-type]
Priority = Literal["High", "Medium", "Low"]
Sentiment = Literal[tuple(value.api for value in s.SENTIMENTS)]  # type: ignore[valid-type]
Urgency = Literal[tuple(value.api for value in s.URGENCIES)]  # type: ignore[valid-type]
Language = Literal[tuple(value.api for value in s.LANGUAGES)]  # type: ignore[valid-type]
Script = Literal[tuple(value.api for value in s.SCRIPTS)]  # type: ignore[valid-type]
StaffAction = Literal[tuple(value.api for value in s.STAFF_ACTIONS)]  # type: ignore[valid-type]
EvidenceKind = Literal[tuple(value.api for value in s.EVIDENCE_KINDS)]  # type: ignore[valid-type]
REF_PATTERN = r"^[A-Za-z0-9_.:\-]{1,70}$"
OPERATION_ID_PATTERN = r"^[A-Za-z0-9_.:\-]{8,64}$"


def salesforce_time(value: datetime) -> str:
    """2026-11-18T09:00:00.000Z, the form the gateway parses and returns."""
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = Field(pattern=REF_PATTERN, description="Source message ID or fact reference.")
    kind: EvidenceKind
    text: str = Field(min_length=1, max_length=32000)
    language: Language | None = None
    script: Script | None = None
    verified_source: str | None = Field(default=None, max_length=255)
    captured_at: str | None = None


class CaseCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(pattern=OPERATION_ID_PATTERN)
    operation: Operation
    customer_id: str
    conversation_id: str | None = Field(default=None, max_length=64)
    case_id: str | None = None
    expected_version: str | None = None
    issue_id: str | None = None
    order_number: str | None = None
    category: Category | None = None
    subject: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=32000)
    language: Language | None = None
    sentiment: Sentiment | None = None
    urgency: Urgency | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    needs_review: bool | None = None
    priority: Priority | None = None
    priority_reason: str | None = Field(default=None, max_length=255)
    staff_action: StaffAction | None = None
    confirmation_message_id: str | None = None
    evidence: tuple[Evidence, ...] = ()

    def body(self) -> bytes:
        """Canonical JSON: the same command always produces the same bytes."""
        def value(item: Any) -> Any:
            if isinstance(item, Decimal):
                return float(item)
            if isinstance(item, BaseModel):
                return {_camel(k): value(v) for k, v in item.model_dump(exclude_none=True).items()}
            if isinstance(item, (list, tuple)):
                return [value(v) for v in item]
            return item

        payload = {_camel(name): value(getattr(self, name)) for name in type(self).model_fields
                   if getattr(self, name) is not None and getattr(self, name) != ()}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class CommandResult(BaseModel):
    """What the gateway decided. ``unknown`` only answers a receipt lookup."""

    model_config = ConfigDict(frozen=True)

    status: Literal["confirmed", "conflict", "failed", "unknown"]
    reason: str | None = None
    message: str | None = None
    operation_id: str | None = None
    operation: str | None = None
    recorded: bool = False
    replayed: bool = False
    case_id: str | None = None
    case_number: str | None = None
    case_version: str | None = None
    case_status: str | None = None
    queue: str | None = None
    overrides: tuple[str, ...] = ()
    evidence_added: int = 0

    @classmethod
    def from_gateway(cls, payload: dict[str, Any]) -> CommandResult:
        names = {_camel(name): name for name in cls.model_fields}
        values = {names[key]: item for key, item in payload.items() if key in names and item is not None}
        return cls.model_validate(values)


class SalesforceCommands:
    """Sends commands to the gateway as the integration user."""

    def __init__(self, client: SalesforceClient):
        self.client = client

    async def execute(self, command: CaseCommand) -> CommandResult:
        payload = await self.client.request("POST", GATEWAY_PATH, raw_json=command.body())
        return CommandResult.from_gateway(payload or {})

    async def receipt(self, operation_id: str, customer_id: str) -> CommandResult:
        """The stored outcome of an earlier command, or status ``unknown`` if it never committed."""
        payload = await self.client.request("GET", f"{GATEWAY_PATH}/{operation_id}",
                                            params={"customerId": customer_id})
        return CommandResult.from_gateway(payload or {})
