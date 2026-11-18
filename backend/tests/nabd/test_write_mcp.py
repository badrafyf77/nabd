"""The write MCP server over Streamable HTTP and the gateway client, without Salesforce. Free."""
import asyncio
import json

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from nabd import context
from nabd.salesforce.commands import GATEWAY_PATH, CaseCommand, CommandResult, Evidence, SalesforceCommands
from nabd.salesforce.rest import SalesforceClient, SalesforceUnavailable
from nabd_fakes import (
    SECRET, WRITE_SECRET, WRITE_URL, FakeGateway, FixtureReads, running, write_app, write_token,
)

CREATE = {
    "operation_id": "op-test-00000001", "issue_id": "nabd-issue-test-0001", "category": "Delivery",
    "subject": "Pot SE-1061 late", "summary": "Customer says the pot is late.", "priority": "Low",
    "priority_reason": "Low: no stronger signal", "sentiment": "negative", "urgency": "low", "confidence": 0.9,
    "needs_review": False, "order_number": "SE-1061",
    "evidence": [{"ref": "m-1", "kind": "customer_report", "text": "My pot is late."}],
}


async def _session(gateway, bearer):
    app = write_app(gateway)
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8092",
                              headers={"Authorization": f"Bearer {bearer}"} if bearer else {})
    return app, http


async def _call(gateway, bearer, tool, args):
    app, http = await _session(gateway, bearer)
    async with running(app), http:
        async with Client(streamable_http_client(WRITE_URL, http_client=http), cache=None) as client:
            result = await client.call_tool(tool, args)
    text = result.content[0].text
    return result.is_error, (text if result.is_error else json.loads(text))


def test_only_write_contexts_signed_with_the_write_secret_are_accepted():
    async def status(bearer):
        app, http = await _session(FakeGateway(FixtureReads()), bearer)
        async with running(app), http:
            response = await http.post(WRITE_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                       headers={"Accept": "application/json, text/event-stream"})
            return response.status_code

    read_token = context.mint(SECRET, customer_id="C003", conversation_id="c", run_id="r", ttl_seconds=60)
    wrong_secret = context.mint(SECRET, customer_id="C003", conversation_id="c", run_id="r", ttl_seconds=60,
                                scopes=(context.WRITE_SCOPE,), audience=context.WRITE_AUDIENCE)
    no_scope = context.mint(WRITE_SECRET, customer_id="C003", conversation_id="c", run_id="r", ttl_seconds=60,
                            audience=context.WRITE_AUDIENCE)
    assert [asyncio.run(status(b)) for b in (None, read_token, wrong_secret, no_scope)] == [401, 401, 401, 403]
    assert asyncio.run(status(write_token("C003"))) == 200


def test_the_customer_and_conversation_come_from_the_token_only():
    gateway = FakeGateway(FixtureReads())
    error, result = asyncio.run(_call(gateway, write_token("C003", conversation_id="conv-9"), "create_case", CREATE))
    assert not error and result["status"] == "confirmed"
    [command] = gateway.calls
    assert (command.customer_id, command.conversation_id) == ("C003", "conv-9")
    # No tool has a customer argument: an injected one is ignored and the token's customer is used.
    asyncio.run(_call(gateway, write_token("C003"), "create_case",
                      {**CREATE, "operation_id": "op-test-00000002", "issue_id": "nabd-issue-test-0002",
                       "customer_id": "C001"}))
    assert gateway.calls[-1].customer_id == "C003"
    assert all(c.customer_id == "C003" for c in FixtureReads.customer_cases(gateway.reads, "C003")
               if c.issue_id and c.issue_id.startswith("nabd-issue-test"))


def test_arguments_are_schema_checked_before_salesforce():
    gateway = FakeGateway(FixtureReads())
    for bad in ({"category": "Shipping"}, {"priority": "Urgent"}, {"operation_id": "x"},
                {"evidence": [{"ref": "m 1", "kind": "customer_report", "text": "x"}]}):
        error, _ = asyncio.run(_call(gateway, write_token("C003"), "create_case", {**CREATE, **bad}))
        assert error
    assert gateway.calls == []


def test_salesforce_silence_is_reported_as_ambiguous_and_the_receipt_can_be_checked():
    gateway = FakeGateway(FixtureReads())
    gateway.fail = ["after"]
    error, text = asyncio.run(_call(gateway, write_token("C003"), "create_case", CREATE))
    assert error and "[unavailable]" in text and "get_operation" in text
    error, receipt = asyncio.run(_call(gateway, write_token("C003"), "get_operation",
                                       {"operation_id": CREATE["operation_id"]}))
    assert not error and receipt["status"] == "confirmed" and receipt["replayed"] is True
    error, other = asyncio.run(_call(gateway, write_token("C001"), "get_operation",
                                     {"operation_id": CREATE["operation_id"]}))
    assert other["status"] == "unknown"  # another customer cannot see it


def test_command_bodies_are_canonical_and_results_parse():
    evidence = (Evidence(ref="m-1", kind="customer_report", text="Late."),)
    first = CaseCommand(operation_id="op-test-00000001", operation="create_case", customer_id="C003",
                        category="Delivery", confidence="0.900", evidence=evidence, subject="Pot")
    again = CaseCommand.model_validate(json.loads(first.model_dump_json()))
    assert first.body() == again.body()
    body = json.loads(first.body())
    assert list(body) == sorted(body) and body["operationId"] == "op-test-00000001"
    assert body["evidence"] == [{"kind": "customer_report", "ref": "m-1", "text": "Late."}]
    result = CommandResult.from_gateway({"status": "conflict", "reason": "version_changed", "caseNumber": "00001001",
                                         "caseVersion": "2026-11-18T09:00:00.000Z", "overrides": [],
                                         "customerId": "C003", "recorded": True, "evidenceAdded": 0})
    assert (result.status, result.case_number, result.recorded) == ("conflict", "00001001", True)


def test_the_gateway_client_sends_exact_bytes_and_treats_server_errors_as_ambiguous():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "unknown", "operationId": "op-test-00000001"})
        if len(seen) == 1:
            return httpx.Response(200, json={"status": "confirmed", "caseNumber": "00002001", "caseId": "500x"})
        return httpx.Response(500, json=[{"errorCode": "APEX_ERROR", "message": "Unable to lock row"}])

    class Token:
        async def token(self, *, refresh=False):
            from pydantic import SecretStr

            from nabd.salesforce.rest import AccessToken
            return AccessToken(SecretStr("t"), "https://example.my.salesforce.com")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            commands = SalesforceCommands(SalesforceClient(Token(), http, "68.0"))
            command = CaseCommand(operation_id="op-test-00000001", operation="create_case", customer_id="C003",
                                  category="Delivery")
            confirmed = await commands.execute(command)
            with pytest.raises(SalesforceUnavailable):
                await commands.execute(command)
            unknown = await commands.receipt("op-test-00000001", "C003")
            return command, confirmed, unknown

    command, confirmed, unknown = asyncio.run(scenario())
    assert confirmed.case_number == "00002001" and unknown.status == "unknown"
    assert seen[0].url.path == GATEWAY_PATH and seen[0].content == command.body() == seen[1].content
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[2].url.params["customerId"] == "C003"
