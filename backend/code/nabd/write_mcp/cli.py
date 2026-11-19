"""``python -m nabd write-mcp``: serve the write MCP server, or smoke-test it against Salesforce.

``smoke`` runs the real server in-process with the real Salesforce settings in
``backend/.env.nabd`` and a throwaway context secret. It writes one clearly
labelled test case for demo customer C008 (Yusuf) and closes it. Every
operation ID is fixed and each request is built from earlier stored results, so
a second run replays the same receipts instead of creating anything new.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from nabd import context
from nabd.settings import WriteMcpSettings

SMOKE = "nabd-smoke-v1"
CUSTOMER = "C008"
AT = "2026-11-18T09:00:00.000Z"


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("write-mcp", help="run or check the case operations MCP server")
    actions = parser.add_subparsers(dest="action")
    serve = actions.add_parser("serve", help="serve Streamable HTTP (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    actions.add_parser("smoke", help="idempotent case write checks through the real gateway")


def run(args: argparse.Namespace) -> int:
    if (args.action or "serve") == "serve":
        import uvicorn

        from nabd.write_mcp.server import create_app

        settings = WriteMcpSettings()
        uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                    log_level="info")
        return 0
    return asyncio.run(_smoke())


def _evidence(ref: str, kind: str, text: str, source: str | None = None) -> dict[str, Any]:
    item = {"ref": ref, "kind": kind, "text": text, "language": "en", "script": "latin", "captured_at": AT}
    if source:
        item["verified_source"] = source
    return item


async def _smoke() -> int:
    from nabd.write_mcp.server import create_app

    secret = SecretStr(secrets.token_urlsafe(32))
    settings = WriteMcpSettings(context_secret=secret, url="http://127.0.0.1:8092/mcp")
    app = create_app(settings)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append((name, passed, detail))

    def http(bearer: str | None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8092",
                                  headers={"Authorization": f"Bearer {bearer}"} if bearer else {})

    def token(customer: str) -> str:
        return context.mint(secret, customer_id=customer, conversation_id="write-smoke", run_id="write-smoke",
                            ttl_seconds=120, scopes=(context.WRITE_SCOPE,), audience=context.WRITE_AUDIENCE)

    @asynccontextmanager
    async def client(bearer: str):
        async with http(bearer) as transport:
            async with Client(streamable_http_client(settings.url, http_client=transport), cache=None) as session:
                yield session

    async def tool(session, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        result = await session.call_tool(name, args)
        text = result.content[0].text if result.content else ""
        return ({}, text) if result.is_error else (json.loads(text), "")

    def summary(result: dict[str, Any]) -> str:
        return f"{result.get('status')} {result.get('reason') or ''} {result.get('case_number') or ''}".strip()

    async with app.router.lifespan_context(app):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        accept = {"Accept": "application/json, text/event-stream"}
        async with http(None) as raw:
            response = await raw.post(settings.url, json=rpc, headers=accept)
            check("request without context rejected", response.status_code == 401, str(response.status_code))
        read_token = context.mint(secret, customer_id=CUSTOMER, conversation_id="x", run_id="x", ttl_seconds=60)
        async with http(read_token) as raw:
            response = await raw.post(settings.url, json=rpc, headers=accept)
            check("read-audience context rejected", response.status_code == 401, str(response.status_code))
        expired = context.mint(secret, customer_id=CUSTOMER, conversation_id="x", run_id="x", ttl_seconds=1,
                               scopes=(context.WRITE_SCOPE,), audience=context.WRITE_AUDIENCE,
                               now=time.time() - 60)
        async with http(expired) as raw:
            response = await raw.post(settings.url, json=rpc, headers=accept)
            check("expired context rejected", response.status_code == 401, str(response.status_code))

        create_args = {
            "operation_id": f"{SMOKE}-create", "issue_id": f"{SMOKE}-issue", "category": "Orders_Products",
            "subject": "NABD write smoke test: backpack strap", "order_number": "SE-1112",
            "summary": "Write-path smoke check created by python -m nabd write-mcp smoke. Not a real complaint.",
            "priority": "Low", "priority_reason": "Low: smoke test", "sentiment": "neutral", "urgency": "low",
            "confidence": 0.99, "needs_review": False,
            "evidence": [_evidence("smoke-msg-0001", "customer_report",
                                   "Smoke test: the strap of my backpack (SE-1112) came loose.")],
        }
        async with client(token(CUSTOMER)) as session:
            names = {item.name for item in (await session.list_tools()).tools}
            check("seven case operation tools listed", len(names) == 7, ", ".join(sorted(names)))
            created, error = await tool(session, "create_case", create_args)
            check("create confirmed (or replayed from an earlier smoke run)",
                  created.get("status") == "confirmed" and created.get("queue") == "NABD_Orders_Products",
                  error or summary(created))
            again, error = await tool(session, "create_case", create_args)
            check("repeated create returns the stored receipt",
                  again.get("replayed") is True and again.get("case_id") == created.get("case_id"),
                  error or summary(again))
            reused, error = await tool(session, "create_case", {**create_args, "subject": "Something else"})
            check("operation ID reused for another request is refused",
                  reused.get("reason") == "operation_reused" and reused.get("recorded") is False,
                  error or summary(reused))
            stale, error = await tool(session, "update_case", {
                "operation_id": f"{SMOKE}-stale", "case_id": created.get("case_id"),
                "expected_version": "2020-01-01T00:00:00.000Z",
                "evidence": [_evidence("smoke-msg-0002", "customer_report", "Smoke test: still loose.")]})
            check("stale version is a conflict", stale.get("reason") == "version_changed", error or summary(stale))
            updated, error = await tool(session, "update_case", {
                "operation_id": f"{SMOKE}-update", "case_id": created.get("case_id"),
                "expected_version": created.get("case_version"), "sentiment": "negative",
                "evidence": [_evidence("smoke-msg-0002", "customer_report", "Smoke test: still loose.")]})
            check("update with the read version confirmed", updated.get("status") == "confirmed",
                  error or summary(updated))
            reopen, error = await tool(session, "reopen_case", {
                "operation_id": f"{SMOKE}-reopen-open", "case_id": created.get("case_id"),
                "expected_version": updated.get("case_version"),
                "evidence": [_evidence("smoke-msg-0003", "customer_report", "Smoke test: reopen?")]})
            check("reopening an open case is refused", reopen.get("reason") == "case_open", error or summary(reopen))

        async with client(token("C001")) as session:
            intruder, error = await tool(session, "update_case", {
                "operation_id": f"{SMOKE}-intruder", "case_id": created.get("case_id"),
                "expected_version": created.get("case_version"),
                "evidence": [_evidence("smoke-msg-9999", "customer_report", "Not my case.")]})
            check("another customer's case is not found", intruder.get("reason") == "case_not_found",
                  error or summary(intruder))
            lookup, error = await tool(session, "get_operation", {"operation_id": f"{SMOKE}-create"})
            check("another customer cannot read the receipt", lookup.get("status") == "unknown",
                  error or summary(lookup))

        async with client(token(CUSTOMER)) as session:
            closed, error = await tool(session, "close_case", {
                "operation_id": f"{SMOKE}-close", "case_id": created.get("case_id"),
                "expected_version": updated.get("case_version"), "confirmation_message_id": "smoke-msg-0004",
                "sentiment": "positive",
                "evidence": [
                    _evidence("smoke-msg-0004", "customer_confirmation", "Smoke test: fixed, please close it."),
                    _evidence("smoke-fact-0001", "verified_fact", "Smoke test fact: order SE-1112 was delivered.",
                              source="ShopEasy_Order__c:SE-1112"),
                ]})
            check("close with confirmation and resolution evidence confirmed",
                  closed.get("status") == "confirmed" and closed.get("case_status") == "Closed",
                  error or summary(closed))
            revise_args = {
                "operation_id": f"{SMOKE}-summary", "case_id": closed.get("case_id"),
                "expected_version": closed.get("case_version"),
                "summary": "Smoke test: the customer reported a loose backpack strap and later confirmed it was fixed.",
            }
            revised, error = await tool(session, "revise_case_summary", revise_args)
            check("summary revision preserves closed status and adds no evidence",
                  revised.get("status") == "confirmed" and revised.get("case_status") == "Closed"
                  and revised.get("evidence_added") == 0, error or summary(revised))
            replay, error = await tool(session, "revise_case_summary", revise_args)
            check("summary revision replay returns the same receipt",
                  replay.get("replayed") is True and replay.get("case_version") == revised.get("case_version"),
                  error or summary(replay))
            lookup, error = await tool(session, "get_operation", {"operation_id": f"{SMOKE}-create"})
            check("receipt lookup returns the stored outcome",
                  lookup.get("status") == "confirmed" and lookup.get("case_number") == created.get("case_number"),
                  error or summary(lookup))
            missing, error = await tool(session, "get_operation", {"operation_id": f"{SMOKE}-never-sent"})
            check("an operation that never ran is unknown", missing.get("status") == "unknown",
                  error or summary(missing))

    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not passed else ""))
    print(f"Smoke case {created.get('case_number')} for {CUSTOMER} "
          f"({'replayed' if created.get('replayed') else 'created'} this run).")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1
