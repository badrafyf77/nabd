"""Read smoke checks against the real org, as the restricted integration user.

Each check states one property the backend relies on: authentication works,
reads are customer-scoped, drafts never leave Salesforce, internal fields and
writes are refused by the platform, and expired/invalid credentials fail
cleanly without exposing secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
from pydantic import SecretStr

from nabd.salesforce import fixtures as fx
from nabd.salesforce import schema as s
from nabd.salesforce.reads import SalesforceReads
from nabd.salesforce.rest import (
    AccessToken, ClientCredentialsAuth, SalesforceAuthError, SalesforceClient, SalesforceRequestError,
)
from nabd.settings import SalesforceSettings


@dataclass
class SmokeResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SmokeContext:
    settings: SalesforceSettings
    http: httpx.AsyncClient
    auth: ClientCredentialsAuth
    client: SalesforceClient
    reads: SalesforceReads
    results: list[SmokeResult] = field(default_factory=list)


def _expected_published(entry_type: str) -> dict[str, int]:
    return {entry.entry_key: entry.revision for entry in fx.build().knowledge
            if entry.status == "Published" and entry.entry_type == entry_type}


async def _authentication(ctx: SmokeContext) -> tuple[bool, str]:
    token = await ctx.auth.token()
    return token.instance_url == ctx.settings.domain_url, f"instance {token.instance_url}"


async def _customer_profile(ctx: SmokeContext) -> tuple[bool, str]:
    sara = await ctx.reads.get_customer("C001")
    camille = await ctx.reads.get_customer("C014")
    ok = (sara is not None and (sara.first_name, sara.last_name, sara.preferred_language) == ("Sara", "Benali", "en")
          and camille is not None and camille.preferred_language == "fr")
    return ok, f"C001={sara and sara.last_name}, C014 language={camille and camille.preferred_language}"


async def _scoped_orders(ctx: SmokeContext) -> tuple[bool, str]:
    sara = [order.order_number for order in await ctx.reads.list_orders("C001")]
    james = sorted(order.order_number for order in await ctx.reads.list_orders("C002"))
    return sara == ["SE-1042"] and james == ["SE-1051", "SE-1052", "SE-1053"], f"C001={sara} C002={james}"


async def _order_detail(ctx: SmokeContext) -> tuple[bool, str]:
    order = await ctx.reads.get_order("C005", "SE-1081")
    ok = (order is not None and len(order.items) == 2 and order.delivery is not None
          and order.delivery.status == "delivered" and [item.returnable for item in order.items] == [False, True])
    return ok, f"items={order and len(order.items)} delivery={order and order.delivery and order.delivery.status}"


async def _other_customers_order_hidden(ctx: SmokeContext) -> tuple[bool, str]:
    leaked = await ctx.reads.get_order("C001", "SE-1051")  # SE-1051 belongs to C002
    return leaked is None, "C001 cannot read SE-1051"


async def _cases(ctx: SmokeContext) -> tuple[bool, str]:
    daniel = await ctx.reads.list_cases("C004")
    sara = await ctx.reads.list_cases("C001")
    ok = [(case.status, case.category, case.order_number) for case in daniel] == [
        ("Working", "Delivery", "SE-1071")] and sara == []
    return ok, f"C004={[(c.case_number, c.status) for c in daniel]} C001={len(sara)}"


async def _published_knowledge(ctx: SmokeContext) -> tuple[bool, str]:
    faqs = {entry.entry_key: entry.revision for entry in await ctx.reads.list_published_knowledge("faq")}
    policies = {entry.entry_key: entry.revision for entry in await ctx.reads.list_published_knowledge("policy")}
    hours = await ctx.reads.get_published_knowledge("faq.support_hours")
    ok = (faqs == _expected_published("faq") and policies == _expected_published("policy")
          and hours is not None and hours.revision == 1)
    return ok, f"faq={len(faqs)} policy={len(policies)} support_hours revision={hours and hours.revision}"


async def _drafts_hidden_by_platform(ctx: SmokeContext) -> tuple[bool, str]:
    # A raw query, bypassing the adapter's filter: sharing must still hide drafts/retired revisions.
    rows = await ctx.client.query(f"SELECT Status__c FROM {s.KNOWLEDGE.name} WHERE Status__c != 'Published'")
    return rows == [], f"{len(rows)} non-published revisions visible"


async def _internal_fields_refused(ctx: SmokeContext) -> tuple[bool, str]:
    try:
        await ctx.client.query("SELECT Sentiment__c FROM Case LIMIT 1")
    except SalesforceRequestError as error:
        return "INVALID_FIELD" in error.codes, f"refused ({', '.join(error.codes)})"
    return False, "integration user could read internal sentiment"


async def _writes_refused(ctx: SmokeContext) -> tuple[bool, str]:
    sara = await ctx.reads.get_customer("C001")
    try:  # An unchanged value: harmless even if the permission were wrongly granted.
        await ctx.client.request("PATCH", f"{ctx.client.base_path}/sobjects/Contact/{sara.record.id}",
                                 json_body={"MailingCity": sara.city})
    except SalesforceRequestError as error:
        return True, f"refused ({', '.join(error.codes)})"
    return False, "integration user could update a contact"


async def _expired_session_refreshes(ctx: SmokeContext) -> tuple[bool, str]:
    current = await ctx.auth.token()
    ctx.auth._cached = AccessToken(SecretStr("expired-session-for-smoke-test"), current.instance_url)
    customer = await ctx.reads.get_customer("C001")
    refreshed = await ctx.auth.token()
    ok = customer is not None and refreshed.value.get_secret_value() != "expired-session-for-smoke-test"
    return ok, "rejected session replaced by a new token"


async def _bad_secret_fails_cleanly(ctx: SmokeContext) -> tuple[bool, str]:
    wrong = "wrong-secret-for-smoke-test"
    auth = ClientCredentialsAuth(ctx.settings.domain_url, ctx.settings.client_id, SecretStr(wrong), ctx.http)
    try:
        await auth.token()
    except SalesforceAuthError as error:
        text = f"{error} {error!r}"
        leaked = wrong in text or ctx.settings.client_id.get_secret_value() in text
        return not leaked, f"{error}"
    return False, "a wrong client secret was accepted"


CHECKS: tuple[tuple[str, Callable[[SmokeContext], Awaitable[tuple[bool, str]]]], ...] = (
    ("client credentials authentication", _authentication),
    ("customer profile and language", _customer_profile),
    ("orders scoped to the requested customer", _scoped_orders),
    ("order items and delivery", _order_detail),
    ("another customer's order is invisible", _other_customers_order_hidden),
    ("customer case summaries", _cases),
    ("current published knowledge only", _published_knowledge),
    ("platform hides draft and retired knowledge", _drafts_hidden_by_platform),
    ("internal case fields refused", _internal_fields_refused),
    ("integration writes refused", _writes_refused),
    ("expired session is refreshed", _expired_session_refreshes),
    ("invalid secret fails without leaking", _bad_secret_fails_cleanly),
)


async def run(settings: SalesforceSettings) -> list[SmokeResult]:
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as http:
        auth = ClientCredentialsAuth(settings.domain_url, settings.client_id, settings.client_secret, http)
        client = SalesforceClient(auth, http, settings.api_version)
        ctx = SmokeContext(settings, http, auth, client, SalesforceReads(client))
        for name, check in CHECKS:
            try:
                passed, detail = await check(ctx)
            except Exception as error:  # report every check; never print credentials
                passed, detail = False, f"{type(error).__name__}: {error}"
            ctx.results.append(SmokeResult(name, passed, detail))
        return ctx.results
