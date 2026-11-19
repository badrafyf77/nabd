"""Browser-check stack: the real API, read and write MCP servers, Salesforce and the dev
PostgreSQL database, with scripted (free) models instead of OpenAI.

Used for the Phase 7 demonstration and staff follow-up browser check; no real model calls. From backend/:

    .venv-text/bin/python scripts/browser_stack.py
    cd ../frontend && NABD_API_ORIGIN=http://127.0.0.1:8083 npm run dev -- --port 3001

Serves the API on 8083, read MCP on 8093 and write MCP on 8094 with throwaway
context secrets. It migrates the selected database. Use a separate
NABD_DATABASE_URL alongside the regular API so each can hold a staff subscriber
lease. The default story is Omar's coffee grinder (C006, SE-1091). The first
complaint creates a sandbox Delivery case and subsequent turns update it.
Close demo cases in Salesforce after the walkthrough.
Pass --recording for Amina's delayed cooking pot (C003, SE-1061). Each
recording-mode launch sets a fresh case subject, so the first complaint creates
a separate case and later turns reuse it. Both stories include the real staff
subscriber and a scripted French translation of the demo comment. No OpenAI
calls.
"""
import argparse
import asyncio
import json
import re
import secrets
import sys

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND / "code"), str(BACKEND / "tests")]

import uvicorn  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from fakes import FakeChatModel, call, say  # noqa: E402
from nabd.agents.quality import Evaluation
from nabd.agents.mcp_tools import http_connector  # noqa: E402
from nabd.agents.models import AgentModels  # noqa: E402
from nabd.app import ChatDependencies, create_app  # noqa: E402
from nabd.migrations import migrate  # noqa: E402
from nabd.read_mcp.server import create_app as read_app  # noqa: E402
from nabd.salesforce.pubsub import PubSubClient
from nabd.salesforce.reads import SalesforceReads  # noqa: E402
from nabd.salesforce.rest import ClientCredentialsAuth, SalesforceClient  # noqa: E402
from nabd.settings import (  # noqa: E402
    BusinessSettings, ChatSettings, QualitySettings, ReadMcpSettings, SalesforceSettings, Settings, WriteMcpSettings,
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


@dataclass(frozen=True)
class DemoStory:
    order_number: str
    item: str
    title: str
    complaint_pattern: str
    forms: dict[str, tuple[str, str]]
    french_staff_reply: str


COFFEE_GRINDER = DemoStory(
    order_number="SE-1091",
    item="coffee grinder",
    title="Coffee grinder SE-1091 not delivered (Phase 7 browser verification)",
    complaint_pattern=r"grinder|SE-1091|late|still|never|arrive",
    forms=BROWSER_FORMS,
    french_staff_reply="L’équipe a traité le remboursement GBP 34.99. Il arrivera dans 3 jours.",
)
COOKING_POT = DemoStory(
    order_number="SE-1061",
    item="cast iron cooking pot",
    title="Delayed delivery for cast iron cooking pot SE-1061",
    complaint_pattern=r"\bSE-1061\b",
    forms={
        "La même cocotte n’est toujours pas arrivée. Ajoutez cette information au même dossier.": ("fr", "latin"),
        "نفس طنجرة الحديد مازال ما وصلاتش، زيد هادشي فنفس الملف عافاك.": ("ary", "arabic"),
    },
    french_staff_reply="Nous avons demandé au transporteur de localiser votre colis. Nous vous tiendrons au courant ici.",
)

CASE_SECTION = "The customer's cases from Salesforce (list_case_history):\n"
BAD_SUMMARY_SENTENCE = "The carrier paid GBP 999 in compensation."


def complaints(messages, *, story=COFFEE_GRINDER):
    reported = f"The customer reports that their {story.item} has not arrived."
    if messages[0].content.startswith("Write one concise English case summary"):
        return say(json.dumps({"language": "en", "summary": reported}))
    request = messages[1].content
    current = re.search(r"Current customer message: \[customer ([0-9a-f-]+)\] (.*)", request)
    message_id, text = current.group(1), current.group(2)
    if text not in story.forms and not re.search(story.complaint_pattern, text, re.I):
        return assessment()
    if not any(isinstance(m, ToolMessage) for m in messages):
        return call("get_order", order_number=story.order_number)
    order = next(json.loads(m.content) for m in reversed(messages) if isinstance(m, ToolMessage))
    delivery = order["delivery"]
    status = delivery["status"].replace("_", " ")
    statement = f"Order {story.order_number} is {status}; expected delivery is {delivery['expected']}."
    fact = {"statement": statement, "evidence": order["evidence"][:2]}
    language, script = story.forms.get(text, ("en", "latin"))

    history = json.JSONDecoder().raw_decode(request.rsplit(CASE_SECTION, 1)[-1])[0]
    existing = next((case for case in history["cases"]
                     if case["subject"] == story.title and case["status"] != "Closed"), None)
    details = {
        "title": story.title,
        "order_numbers": [story.order_number],
        "customer_message_ids": [message_id],
        "verified_facts": [fact],
    }
    summary = f"{reported} {statement}"
    if existing:
        item = issue(**details, existing_case=existing["case_number"], match="same", action="update",
                     urgency="medium", english_summary=summary)
    else:
        item = issue(**details, urgency="low", sentiment="negative",
                     english_summary=f"{summary} {BAD_SUMMARY_SENTENCE}")
    return localized_answer(assessment(item), language, script, message_id)


def supervisor(messages, *, story=COFFEE_GRINDER):
    if messages[0].content.startswith("Translate a ShopEasy staff reply"):
        return say(json.dumps({"text": story.french_staff_reply, "source_language": "en"}, ensure_ascii=False))
    current = next((m.text for m in reversed(messages) if m.type == "human"), "")
    if current == "How much does delivery cost?":
        if not isinstance(messages[-1], ToolMessage):
            return call("get_knowledge", entry_keys=["faq.delivery_cost"])
        data = json.loads(messages[-1].content)
        return say(data["entries"][0]["body"] if data.get("entries") else "Delivery information is unavailable.")
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


def quality(messages):
    data = json.loads(messages[1].content)
    summaries = []
    for case in data["confirmed_cases"]:
        if summary := case["summary"]:
            bad = BAD_SUMMARY_SENTENCE in summary
            summaries.append({"source_command": case["source_command"],
                              "verdict": "needs_correction" if bad else "grounded",
                              "unsupported": [BAD_SUMMARY_SENTENCE] if bad else [], "missing": []})
    grade = Evaluation(coverage="pass", language="pass", tool_use="pass", resolution_claims="none",
                       grounding="pass", findings=[], summaries=summaries)
    return say(grade.model_dump_json())


async def main(story):
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

    models = AgentModels(Callable(fn=partial(supervisor, story=story)), FakeChatModel(),
                         Callable(fn=partial(complaints, story=story)))
    deps = ChatDependencies(
        reads=reads, models=models, connect=http_connector(READ.url, 20), read_mcp=READ,
        chat=ChatSettings(allowed_origins="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"), business=BusinessSettings(), connect_writes=http_connector(WRITE.url, 30),
        write_mcp=WRITE, subscriber=PubSubClient(auth), evaluator=Callable(fn=quality),
        quality=QualitySettings(enabled=True, summary_correction_enabled=True, poll_seconds=0.25))
    servers = [
        uvicorn.Server(uvicorn.Config(read_app(READ), host="127.0.0.1", port=8093, log_level="warning", timeout_graceful_shutdown=3)),
        uvicorn.Server(uvicorn.Config(write_app(WRITE), host="127.0.0.1", port=8094, log_level="warning", timeout_graceful_shutdown=3)),
        uvicorn.Server(uvicorn.Config(create_app(settings, chat=deps), host="127.0.0.1", port=8083,
                                      log_level="info", timeout_graceful_shutdown=3)),
    ]
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", action="store_true", help="Use Amina's order and a fresh case subject.")
    args = parser.parse_args()
    story = COFFEE_GRINDER
    if args.recording:
        story = replace(COOKING_POT, title=f"{COOKING_POT.title} (video {secrets.token_hex(4).upper()})")
        print(f"Recording case subject: {story.title}", flush=True)
    asyncio.run(main(story))
