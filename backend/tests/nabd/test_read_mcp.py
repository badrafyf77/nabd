"""The read MCP server over real Streamable HTTP (in-memory ASGI), with fixture data."""
import asyncio
import json
import time

import pytest
from pydantic import SecretStr

from nabd import context
from nabd.read_mcp.server import return_check
from nabd.salesforce import fixtures as fx
from nabd_fakes import (
    SECRET, FixtureReads, case_token, http_client, mcp_app, read_mcp_client, running, token, unavailable,
)


def run(coro):
    return asyncio.run(coro)


async def _call(reads, bearer, tool, args=None):
    async with running(mcp_app(reads)) as app, read_mcp_client(app, bearer) as client:
        result = await client.call_tool(tool, args or {})
        return result.is_error, (json.loads(result.content[0].text) if not result.is_error
                                 else result.content[0].text)


# --- Signed context ------------------------------------------------------------------


def test_context_tokens_round_trip_and_reject_tampering_expiry_and_audience():
    minted = token("C011", run_id="r-9", conversation_id="c-9")
    claims = context.verify(SECRET, minted)
    assert (claims.customer_id, claims.run_id, claims.conversation_id) == ("C011", "r-9", "c-9")
    assert claims.scopes == (context.READ_SCOPE,)

    version, body, signature = minted.split(".")
    other = context.mint(SECRET, customer_id="C002", conversation_id="c-9", run_id="r-9", ttl_seconds=60)
    forged = ".".join((version, other.split(".")[1], signature))  # C002's claims under C011's signature
    for bad in (forged, minted + "x", "v2." + body + "." + signature, "", "a.b"):
        with pytest.raises(context.InvalidContext):
            context.verify(SECRET, bad)
    with pytest.raises(context.InvalidContext):
        context.verify(SecretStr("another-secret-that-is-long-enough!!"), minted)
    with pytest.raises(context.InvalidContext, match="expired"):
        context.verify(SECRET, token(ttl=1), now=time.time() + 5)
    with pytest.raises(context.InvalidContext, match="audience"):
        context.verify(SECRET, token(audience="nabd-write-mcp"))
    with pytest.raises(ValueError):
        token("C001' OR ''='")


def test_requests_without_a_valid_context_are_rejected_before_any_tool_runs():
    reads = FixtureReads()

    async def main():
        async with running(mcp_app(reads)) as app:
            statuses = []
            for bearer in (None, "not-a-token", token(ttl=1, now=time.time() - 30)):
                async with http_client(app, bearer) as http:
                    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                               headers={"Accept": "application/json, text/event-stream"})
                    statuses.append(response.status_code)
            return statuses

    assert run(main()) == [401, 401, 401]
    assert reads.calls == []


def test_tools_resources_and_prompt_are_listed_with_read_only_tools():
    async def main():
        async with running(mcp_app(FixtureReads())) as app, read_mcp_client(app, token()) as client:
            tools = (await client.list_tools()).tools
            resources = (await client.list_resources()).resources
            templates = (await client.list_resource_templates()).resource_templates
            prompts = (await client.list_prompts()).prompts
            return tools, resources, templates, prompts

    tools, resources, templates, prompts = run(main())
    assert {t.name for t in tools} == {"get_customer_profile", "list_orders", "get_order", "list_cases",
                                       "list_case_history", "list_knowledge", "get_knowledge"}
    assert all(t.annotations.read_only_hint for t in tools)
    assert all("customer" not in json.dumps(t.input_schema.get("properties", {})) for t in tools)
    assert {str(r.uri) for r in resources} == {"nabd://customer/profile", "nabd://knowledge/index"}
    assert [t.uri_template for t in templates] == ["nabd://knowledge/{entry_key}"]
    assert [p.name for p in prompts] == ["staff_review"]


# --- Customer scoping ------------------------------------------------------------------


def test_orders_are_scoped_to_the_signed_customer():
    reads = FixtureReads()
    error, orders = run(_call(reads, token("C001"), "list_orders"))
    assert not error and [o["order_number"] for o in orders["orders"]] == ["SE-1042"]
    assert orders["orders"][0]["products"] and orders["orders"][0]["evidence"][0].startswith("ShopEasy_Order__c:")

    error, other = run(_call(reads, token("C001"), "get_order", {"order_number": "SE-1051"}))  # C002's order
    assert not error and other["found"] is False and "not on the signed-in customer's account" in other["message"]
    assert ("get_order", ("C001", "SE-1051")) in reads.calls


def test_invalid_order_numbers_are_refused_with_a_code():
    error, message = run(_call(FixtureReads(), token("C001"), "get_order", {"order_number": "1042' OR Id != '"}))
    assert error and "[invalid_argument]" in message


@pytest.mark.parametrize(("customer", "order", "can_return", "reason"), [
    ("C002", "SE-1051", True, "until"),
    ("C002", "SE-1053", False, "return window ended"),
    ("C005", "SE-1081", True, "until"),  # the shirt can go back, the boxer briefs never can
])
def test_return_checks_are_computed_from_the_published_policy(customer, order, can_return, reason):
    error, details = run(_call(FixtureReads(), token(customer), "get_order", {"order_number": order}))
    check = details["return_check"]
    if order == "SE-1081":
        assert check["non_returnable_products"] == ["Cotton boxer briefs, 3-pack"]
        assert check["returnable_products"] == ["Linen shirt"]
    assert not error and check["available"] and check["can_return"] is can_return
    assert reason in check["reason"]
    assert check["return_window_days"] == 30 and check["policy_revision"] == "policy.return_window@1"
    assert any(ref.startswith("KnowledgeEntry__c:") for ref in check["evidence"])


def test_return_check_is_unavailable_without_a_published_window():
    reads = FixtureReads()
    order = run(reads.get_order("C002", "SE-1051"))
    check = return_check(order, None, fx.SEED_DATE)
    assert check.available is False and check.can_return is None


def test_knowledge_returns_only_published_revisions_with_their_revision():
    reads = FixtureReads()
    error, index = run(_call(reads, token(), "list_knowledge"))
    keys = {e["key"]: e["revision"] for e in index["entries"]}
    assert not error and keys["faq.delivery_cost"] == 2 and keys["faq.support_hours"] == 1

    error, result = run(_call(reads, token(), "get_knowledge",
                              {"entry_keys": ["faq.delivery_cost", "faq.unknown_entry"]}))
    assert [e["revision_key"] for e in result["entries"]] == ["faq.delivery_cost@2"]
    assert "£2.99" not in result["entries"][0]["body"]  # the retired revision's price
    assert result["missing"] == ["faq.unknown_entry"]


def test_cases_omit_internal_fields():
    error, cases = run(_call(FixtureReads(), token("C011"), "list_cases"))
    assert not error and cases["count"] >= 1
    assert set(cases["cases"][0]) == {"case_number", "subject", "status", "category", "order_number",
                                      "opened", "closed"}


def test_case_history_needs_its_own_scope_and_omits_internal_assessments():
    reads = FixtureReads()
    error, text = run(_call(reads, token("C004"), "list_case_history"))
    assert error and "[not_allowed]" in text
    assert not any(name == "list_case_history" for name, _ in reads.calls)  # refused before Salesforce
    error, history = run(_call(reads, case_token("C004"), "list_case_history"))
    assert not error and history["count"] == 1
    case = history["cases"][0]
    assert case["case_number"] == "00001001" and "SE-1071" in case["summary"]
    assert case["staff_action"] == "carrier_investigation" and case["staff_action_completed"] is False
    assert case["evidence"][0].startswith("Case:500FX")
    assert not set(case) & {"priority", "sentiment", "urgency", "confidence", "needs_review"}
    error, other = run(_call(reads, case_token("C001"), "list_case_history"))
    assert not error and other["count"] == 0  # another customer's cases never appear


def test_salesforce_outages_become_coded_errors_without_details():
    reads = FixtureReads()
    reads.fail_next.append(unavailable())
    error, message = run(_call(reads, token(), "list_orders"))
    assert error and "[unavailable]" in message and "ConnectError" not in message


def test_resources_and_prompt_use_the_signed_customer():
    async def main():
        async with running(mcp_app(FixtureReads())) as app, read_mcp_client(app, token("C011")) as client:
            profile = await client.read_resource("nabd://customer/profile")
            entry = await client.read_resource("nabd://knowledge/policy.refunds")
            prompt = await client.get_prompt("staff_review", {"focus": "a late parcel"})
            return profile, entry, prompt

    profile, entry, prompt = run(main())
    assert json.loads(profile.contents[0].text)["customer_id"] == "C011"
    assert json.loads(entry.contents[0].text)["revision_key"] == "policy.refunds@1"
    text = prompt.messages[0].content.text
    assert "customer C011" in text and "a late parcel" in text and "policy.return_window@1" in text
    assert "C001" not in text
