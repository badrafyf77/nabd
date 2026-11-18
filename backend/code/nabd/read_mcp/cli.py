"""``python -m nabd read-mcp``: serve the read MCP server, render its prompt, smoke-test it.

``smoke`` and ``prompt`` run the real server in-process (no port needed) with
real Salesforce settings from ``backend/.env.nabd``, signing contexts with a
throwaway secret. ``smoke --url`` checks an already running server instead and
then needs ``NABD_READ_MCP_CONTEXT_SECRET``. No model is ever called.
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
from nabd.settings import ReadMcpSettings


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("read-mcp", help="run or check the customer-scoped read MCP server")
    actions = parser.add_subparsers(dest="action")
    serve = actions.add_parser("serve", help="serve Streamable HTTP (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    prompt = actions.add_parser("prompt", help="render the staff_review prompt for one customer")
    prompt.add_argument("--customer", required=True)
    prompt.add_argument("--focus", default="the customer's open questions")
    smoke = actions.add_parser("smoke", help="read checks through the real server and Salesforce")
    smoke.add_argument("--url", help="check a running server instead of an in-process one")


def run(args: argparse.Namespace) -> int:
    action = args.action or "serve"
    if action == "serve":
        import uvicorn

        from nabd.read_mcp.server import create_app

        settings = ReadMcpSettings()
        uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                    log_level="info")
        return 0
    if action == "prompt":
        return asyncio.run(_prompt(args.customer, args.focus))
    return asyncio.run(_smoke(args.url))


@asynccontextmanager
async def _server(url: str | None):
    """Yield (open-client factory, secret) for a running or in-process server."""
    if url:
        secret = ReadMcpSettings().context_secret

        def http(bearer):
            return httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"} if bearer else {})

        yield url, http, secret
        return
    from nabd.read_mcp.server import create_app

    secret = SecretStr(secrets.token_urlsafe(32))
    settings = ReadMcpSettings(context_secret=secret, url="http://127.0.0.1:8091/mcp")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        def http(bearer):
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8091",
                                      headers={"Authorization": f"Bearer {bearer}"} if bearer else {})

        yield settings.url, http, secret


def _token(secret: SecretStr, customer_id: str, **kwargs: Any) -> str:
    return context.mint(secret, customer_id=customer_id, conversation_id="cli", run_id=f"cli-{customer_id}",
                        ttl_seconds=kwargs.pop("ttl", 120), **kwargs)


@asynccontextmanager
async def _client(url: str, http, bearer: str):
    async with http(bearer) as transport:
        async with Client(streamable_http_client(url, http_client=transport), cache=None) as client:
            yield client


async def _prompt(customer_id: str, focus: str) -> int:
    async with _server(None) as (url, http, secret):
        async with _client(url, http, _token(secret, customer_id)) as client:
            result = await client.get_prompt("staff_review", {"focus": focus})
    print(result.messages[0].content.text)
    return 0


async def _smoke(url: str | None) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append((name, passed, detail))

    async def tool(client, name, args=None):
        result = await client.call_tool(name, args or {})
        text = result.content[0].text if result.content else ""
        return (None, text) if result.is_error else (json.loads(text), "")

    async with _server(url) as (endpoint, http, secret):
        async with http(None) as raw:
            response = await raw.post(endpoint, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                      headers={"Accept": "application/json, text/event-stream"})
            check("request without context rejected", response.status_code == 401, str(response.status_code))
        async with http(_token(secret, "C001", ttl=1, now=time.time() - 60)) as raw:
            response = await raw.post(endpoint, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                      headers={"Accept": "application/json, text/event-stream"})
            check("expired context rejected", response.status_code == 401, str(response.status_code))

        async with _client(endpoint, http, _token(secret, "C001")) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            check("seven read-only tools listed", len(names) == 7, ", ".join(sorted(names)))
            history, error = await tool(client, "list_case_history")
            check("case history needs the case-history scope", history is None and "not_allowed" in error, error)
            profile, error = await tool(client, "get_customer_profile")
            check("profile of the signed customer", bool(profile) and profile["customer_id"] == "C001", error)
            orders, error = await tool(client, "list_orders")
            numbers = [o["order_number"] for o in (orders or {}).get("orders", [])]
            check("orders scoped to the customer", numbers == ["SE-1042"], error or ", ".join(numbers))
            other, error = await tool(client, "get_order", {"order_number": "SE-1051"})
            check("another customer's order is not found", bool(other) and other["found"] is False, error)
            index, error = await tool(client, "list_knowledge")
            revisions = {e["key"]: e["revision"] for e in (index or {}).get("entries", [])}
            check("knowledge index has only published revisions",
                  revisions.get("faq.support_hours") == 1 and revisions.get("faq.delivery_cost") == 2,
                  error or f"{len(revisions)} entries")
            article, error = await tool(client, "get_knowledge", {"entry_keys": ["faq.delivery_cost"]})
            entries = (article or {}).get("entries", [])
            check("knowledge cites the current revision",
                  bool(entries) and entries[0]["revision_key"] == "faq.delivery_cost@2", error)
            resource = await client.read_resource("nabd://customer/profile")
            check("profile resource is customer-scoped",
                  json.loads(resource.contents[0].text)["customer_id"] == "C001")

        async with _client(endpoint, http, _token(secret, "C002")) as client:
            order, error = await tool(client, "get_order", {"order_number": "SE-1051"})
            check_ = (order or {}).get("return_check") or {}
            check("return check uses the published window",
                  check_.get("policy_revision") == "policy.return_window@1"
                  and check_.get("return_window_days") == 30, error or json.dumps(check_)[:160])

        async with _client(endpoint, http, _token(secret, "C011")) as client:
            cases, error = await tool(client, "list_cases")
            fields = set((cases or {}).get("cases", [{}])[0]) if (cases or {}).get("cases") else set()
            check("cases expose only public fields",
                  bool(fields) and not fields & {"priority", "sentiment", "summary"}, error or ", ".join(fields))
            prompt = await client.get_prompt("staff_review", {"focus": "smoke"})
            text = prompt.messages[0].content.text
            check("staff_review prompt is customer-scoped", "customer C011" in text and "C001" not in text)
        async with _client(endpoint, http, _token(secret, "C011", scopes=(context.READ_SCOPE,
                                                                       context.CASE_HISTORY_SCOPE))) as client:
            history, error = await tool(client, "list_case_history")
            items = (history or {}).get("cases", [])
            check("case history is scoped and carries summaries without internal fields",
                  bool(items) and all(item.get("summary") for item in items)
                  and not set(items[0]) & {"priority", "sentiment", "urgency", "confidence"},
                  error or f"{len(items)} cases")

    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not passed else ""))
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1
