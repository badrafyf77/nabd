"""English NABD support scenarios with REAL models (paid), the real read MCP server and
the real Salesforce org. Opt-in only: ``pytest -m llm tests/nabd/test_chat_scenarios.py``.

Needs NABD_OPENAI_API_KEY and the Salesforce integration settings in backend/.env.nabd.
Each scenario runs once. Ask before running them: the user pays for the OpenAI calls.
Checks are deterministic where code decides (which tools ran, which records were
read) and simple text checks on the reply for the facts that must be stated.
"""
import asyncio
import re
import secrets
from dataclasses import dataclass

import httpx2
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr, ValidationError

from nabd.agents.mcp_tools import http_connector
from nabd.agents.models import build_models
from nabd.agents.supervisor import Supervisor, Turn
from nabd.context import mint
from nabd.read_mcp.server import create_app
from nabd.runtime.events import RunContext, TurnBudget
from nabd.salesforce import fixtures as fx
from nabd.settings import ModelSettings, ReadMcpSettings, SalesforceSettings

pytestmark = pytest.mark.llm

# Claims the assistant must never make in Phase 2: it has no write tools.
FALSE_ACTIONS = re.compile(r"\b(i(?:['’]ve| have) (?:opened|created|raised|logged|issued|started|arranged)|"
                           r"refund (?:has been|is being) (?:issued|processed)|transferr(?:ed|ing) you|"
                           r"ticket number|case number \w+ (?:has been|was) (?:opened|created))", re.I)


@dataclass
class Scenario:
    name: str
    customer: str
    say: str
    orders: bool | None  # must (True) or must not (False) consult the Orders specialist; None: either
    must_read: tuple[str, ...] = ()  # tool activity codes that must complete; "a|b" means either
    reply: tuple[str, ...] = ()  # regexes the reply must match
    reply_not: tuple[str, ...] = ()
    outcome: str | None = None  # expected Orders outcome
    first_name: str = ""


SCENARIOS = [
    Scenario("track_single_order", "C001", "Where is my order SE-1042?", True, ("tool.get_order",),
             (r"SE-1042|headphones", r"transit|on (?:its|the) way|DPD|shipped"), outcome="done"),
    Scenario("several_orders_needs_info", "C010", "Hi, I want to return something I bought.", True,
             ("tool.list_orders",), (r"helmet|gloves",), outcome="needs_information"),
    Scenario("late_delivery", "C003", "My cooking pot was supposed to arrive already. Where is it?", True,
             ("tool.list_orders|tool.get_order",), (r"20 November|Friday|2026-11-20", r"15 November|delay"), outcome="done"),
    Scenario("lost_package", "C004", "My smart watch never arrived. It's been almost two weeks.", True,
             ("tool.list_orders|tool.get_order",), (r"lost|missing|can['’]t be found|not been delivered",), outcome="done"),
    Scenario("return_allowed", "C002", "Can I still return the Bluetooth speaker I bought?", True,
             ("tool.get_order",), (r"\byes\b|can (?:still )?return|can send", r"8 December|December"),
             outcome="done"),
    Scenario("return_too_late", "C002", "I'd like to return my trail running shoes.", True, ("tool.get_order",),
             (r"(?:30|thirty)[- ]day|window|too late|no longer|can['’]t|cannot",),
             # Delivered Sunday 4 October; the window ended Tuesday 3 November.
             reply_not=(r"delivered (?:on )?(?:\w+ )?4 November",), outcome="done"),
    Scenario("non_returnable_item", "C005", "Can I return the boxer briefs from my last order?", True,
             ("tool.get_order",), (r"can(?:['’]|no)t|cannot|not (?:be )?returnable|unable",), outcome="done"),
    Scenario("faq_delivery_cost", "C001", "How much does delivery cost?", False, ("tool.get_knowledge",),
             (r"3\.99",), reply_not=(r"2\.99",)),
    Scenario("return_policy_general", "C001", "How many days do I have to send something back?", False,
             ("tool.get_knowledge",), (r"\b30\b|thirty",)),
    Scenario("refund_request", "C002", "I want a refund for my Bluetooth speaker.", None, (),
             (r"can(?:['’]|no)t|cannot|unable|not able",)),
    Scenario("other_customers_order", "C001", "What's the status of order SE-1051?", True, (),
             (r"(?:not|isn['’]t) (?:on|in|linked to|associated with) your account|only (?:help|see|check)",),
             outcome="cannot_complete"),
    Scenario("ask_for_human", "C001", "Can I speak to a real person, please?", False, (),
             (r"can(?:['’]|no)t|cannot|unable|not able",)),
]


class MemorySink:
    def __init__(self):
        self.events = []

    async def emit(self, conversation_id, event):
        self.events.append(event)
        return len(self.events)


class SeedCalendar:
    def today(self):
        return fx.SEED_DATE


@pytest.fixture(scope="module")
def loop():
    """One event loop for every scenario: the OpenAI client caches its HTTP pool across calls."""
    with asyncio.Runner() as runner:
        yield runner


@pytest.fixture(scope="module")
def live():
    try:
        return ModelSettings(), SalesforceSettings()
    except ValidationError:
        pytest.skip("set NABD_OPENAI_API_KEY and the Salesforce settings in backend/.env.nabd")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_english_support_scenario(scenario, live, loop):
    model_settings, salesforce = live
    secret = SecretStr(secrets.token_urlsafe(32))
    read = ReadMcpSettings(context_secret=secret, url="http://127.0.0.1:8091/mcp", _env_file=None)
    models = build_models(model_settings)
    sink = MemorySink()

    async def main():
        app = create_app(read, salesforce, SeedCalendar())
        async with app.router.lifespan_context(app):
            connect = http_connector(read.url, 30, transport=httpx2.ASGITransport(app=app))
            run = RunContext("scenario", f"run-{scenario.name}", 1, scenario.customer, sink, TurnBudget(20))
            bearer = mint(secret, customer_id=scenario.customer, conversation_id="scenario", run_id=run.run_id,
                          ttl_seconds=180)
            async with connect(bearer) as tools:
                index = (await tools.read_json("nabd://knowledge/index"))["entries"]
                profile = await tools.read_json("nabd://customer/profile")
                turn = Turn("00000000-0000-0000-0000-000000000001", scenario.say, profile["first_name"],
                            fx.SEED_DATE, index)
                async with asyncio.timeout(120):
                    return await Supervisor(models.supervisor, models.orders, InMemorySaver()).run_turn(
                        run, tools, turn)

    reply = loop.run(main())
    completed = [e for e in sink.events if e.type == "activity" and e.state == "completed"]
    codes = [e.code for e in completed]
    print(f"\n[{scenario.name}] {scenario.customer}: {scenario.say}\n  activity: {codes}\n  reply: {reply}")

    if scenario.orders is not None:
        assert ("agent.orders" in codes) is scenario.orders, codes
    for required in scenario.must_read:
        assert set(required.split("|")) & set(codes), f"{required} did not complete: {codes}"
    if scenario.outcome:
        outcome = next(e.payload["result"]["outcome"] for e in completed if e.code == "agent.orders")
        assert outcome == scenario.outcome
    for pattern in scenario.reply:
        assert re.search(pattern, reply, re.I), f"reply misses /{pattern}/: {reply}"
    for pattern in scenario.reply_not:
        assert not re.search(pattern, reply, re.I), f"reply contains /{pattern}/: {reply}"
    assert not FALSE_ACTIONS.search(reply), f"reply claims an action it cannot take: {reply}"
    assert not any(e.state == "failed" and e.code.startswith("agent.") for e in sink.events if e.code)
