"""Browser-check stack: the real API, read and write MCP servers, Salesforce and the dev
PostgreSQL database, with scripted (free) models instead of OpenAI.

Used for the Phase 5 staff follow-up browser check; no real model calls. From backend/:

    .venv-text/bin/python scripts/browser_stack.py
    cd ../frontend && NABD_API_ORIGIN=http://127.0.0.1:8083 npm run dev -- --port 3001

Serves the API on 8083, read MCP on 8093 and write MCP on 8094 with throwaway
context secrets (stop the Compose `api` first only if you change these ports).
It migrates the dev database. The scripted Complaint model knows one story:
customer C006 (Omar) says coffee grinder SE-1091 has not arrived; a first
message opens a real Delivery case in the org, a follow-up updates it. Close
that test case in Salesforce afterwards (Phase 3 closed 00001044 as withdrawn).
Includes the real staff subscriber and a scripted French translation. Staff
changes/public comments made as the CLI admin appear in the open chat. Adapt
``complaints()``/``supervisor()`` for other stories. No OpenAI calls.
"""
import asyncio
import json
import re
import secrets
import sys

from pathlib import Path  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "code"), str(BACKEND / "tests")]

import uvicorn  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from fakes import FakeChatModel, call, say  # noqa: E402
from nabd.agents.mcp_tools import http_connector  # noqa: E402
from nabd.agents.models import AgentModels  # noqa: E402
from nabd.app import ChatDependencies, create_app  # noqa: E402
from nabd.migrations import migrate  # noqa: E402
from nabd.read_mcp.server import create_app as read_app  # noqa: E402
from nabd.salesforce.pubsub import PubSubClient
from nabd.salesforce.reads import SalesforceReads  # noqa: E402
from nabd.salesforce.rest import ClientCredentialsAuth, SalesforceClient  # noqa: E402
from nabd.settings import (  # noqa: E402
    BusinessSettings, ChatSettings, ReadMcpSettings, SalesforceSettings, Settings, WriteMcpSettings,
)
from nabd.write_mcp.server import create_app as write_app  # noqa: E402
from nabd_fakes import assessment, issue

BROWSER_FORMS = {
    "نفس مطحنة القهوة مازال ما وصلاتش، زيد هادشي فنفس الملف عافاك.": ("ary", "arabic"),
    "Le même moulin à café n’est toujours pas arrivé. Mettez à jour le même dossier.": ("fr", "latin"),
    "Nefs moulin dyal lqahwa mazal ma wselch. Zid hadchi f nefs dossier 3afak.": ("ary", "latin"),
}

def localized_answer(reply, language, script, message_id):
    data = json.loads("".join(reply["text"]))
    data["language"] = {"input_language": language, "reply_language": language, "reply_script": script}
    data["message_languages"] = [{"message_id": message_id, "language": language}]
    for item in data["issues"]:
        item["language"] = language
    return say(json.dumps(data, ensure_ascii=False))  # noqa: E402

READ = ReadMcpSettings(context_secret=SecretStr(secrets.token_urlsafe(32)), url="http://127.0.0.1:8093/mcp",
                       port=8093, allowed_hosts="127.0.0.1:8093")
WRITE = WriteMcpSettings(context_secret=SecretStr(secrets.token_urlsafe(32)), url="http://127.0.0.1:8094/mcp",
                         port=8094, allowed_hosts="127.0.0.1:8094")
TITLE = "Coffee grinder SE-1091 not delivered (Phase 5 browser verification)"


def complaints(messages):
    request = messages[1].content
    current = re.search(r"Current customer message: \[customer ([0-9a-f-]+)\] (.*)", request)
    message_id, text = current.group(1), current.group(2)
    if text not in BROWSER_FORMS and not re.search(r"grinder|SE-1091|late|still|never|arrive", text, re.I):
        return assessment()
    if not any(isinstance(m, ToolMessage) for m in messages):
        return call("get_order", order_number="SE-1091")
    order = next(json.loads(m.content) for m in reversed(messages) if isinstance(m, ToolMessage))
    fact = {"statement": f"SE-1091 delivery status is {order['delivery']['status']}; expected "
                         f"{order['delivery']['expected']}.", "evidence": order["evidence"][:2]}
    language, script = BROWSER_FORMS.get(text, ("en", "latin"))
    def result(item):
        return localized_answer(assessment(item), language, script, message_id)
    existing = re.search(r'"case_number": "(\d+)", "subject": "' + re.escape(TITLE), request)
    if existing:
        return result(issue(title=TITLE, existing_case=existing.group(1), match="same", action="update",
                                order_numbers=["SE-1091"], customer_message_ids=[message_id],
                                verified_facts=[fact], urgency="medium",
                                english_summary="Customer says the coffee grinder SE-1091 still has not "
                                                "arrived. The delivery record shows it out for delivery."))
    return result(issue(title=TITLE, order_numbers=["SE-1091"], customer_message_ids=[message_id],
                            verified_facts=[fact], urgency="low", sentiment="negative",
                            english_summary="Customer says the coffee grinder SE-1091 has not arrived. The "
                                            "delivery record shows it out for delivery, expected today."))


def supervisor(messages):
    if messages[0].content.startswith("Translate a ShopEasy staff reply"):
        return say(json.dumps({"text": "L’équipe a traité le remboursement GBP 34.99. Il arrivera dans 3 jours.",
                               "source_language": "en"}, ensure_ascii=False))
    section = messages[0].content.split("## Case work for this message", 1)[1].split("- When a case", 1)[0]
    number = re.search(r"case (\d+)", section)
    number = number.group(1) if number else ""
    if '"reply_language": "ary"' in messages[0].content and '"reply_script": "arabic"' in messages[0].content:
        return say(f"زدنا هاد المعلومة فالملف {number} عند الفريق ديال التوصيل.")
    if '"reply_language": "ary"' in messages[0].content and '"reply_script": "latin"' in messages[0].content:
        return say(f"Zedna had lma3loma f dossier {number} 3nd l’équipe dyal livraison.")
    if '"reply_language": "fr"' in messages[0].content:
        return say(f"J’ai ajouté cette information au dossier {number} de l’équipe Livraison.")
    lines = [line[2:] for line in section.splitlines() if line.startswith("- ")]
    return say("Thanks for telling me. " + " ".join(lines))


async def main():
    settings = Settings()
    await migrate(settings)
    salesforce = SalesforceSettings()
    import httpx

    http = httpx.AsyncClient(timeout=salesforce.timeout_seconds)
    auth = ClientCredentialsAuth(salesforce.domain_url, salesforce.client_id, salesforce.client_secret, http)
    reads = SalesforceReads(SalesforceClient(auth, http, salesforce.api_version))
    class Callable(FakeChatModel):
        """A scripted model that answers every request with one function."""

        fn: object = None

        def _next(self, messages, kwargs):
            self.requests.append({"messages": list(messages), **kwargs})
            return self.fn(messages)

    models = AgentModels(Callable(fn=supervisor), FakeChatModel(), Callable(fn=complaints))
    deps = ChatDependencies(
        reads=reads, models=models, connect=http_connector(READ.url, 20), read_mcp=READ,
        chat=ChatSettings(allowed_origins="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"), business=BusinessSettings(), connect_writes=http_connector(WRITE.url, 30),
        write_mcp=WRITE, subscriber=PubSubClient(auth))
    servers = [
        uvicorn.Server(uvicorn.Config(read_app(READ), host="127.0.0.1", port=8093, log_level="warning", timeout_graceful_shutdown=3)),
        uvicorn.Server(uvicorn.Config(write_app(WRITE), host="127.0.0.1", port=8094, log_level="warning", timeout_graceful_shutdown=3)),
        uvicorn.Server(uvicorn.Config(create_app(settings, chat=deps), host="127.0.0.1", port=8083,
                                      log_level="info", timeout_graceful_shutdown=3)),
    ]
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
