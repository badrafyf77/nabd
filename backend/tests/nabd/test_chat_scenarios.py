"""English NABD support scenarios (Phase 2 set) with REAL models (paid), run as full
Phase 3 turns: the complaint stage, then the Supervisor, through the real read and write
MCP servers. Orders and knowledge come from the real Salesforce org; cases from the
fixture world (see ``live_turns``). Opt-in only: ``pytest -m llm tests/nabd/test_chat_scenarios.py``.

Needs NABD_OPENAI_API_KEY and the Salesforce integration settings in backend/.env.nabd.
Each scenario runs once. Ask before running them: the user pays for the OpenAI calls.
Checks are deterministic where code decides (which tools ran, which records were
read, which case commands were sent) and simple text checks on the reply.
"""
import asyncio
import re
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from live_turns import CASE_NUMBER, LiveConversation, live_models
from nabd.settings import ModelSettings, SalesforceSettings

pytestmark = pytest.mark.llm

# Claims the assistant must never make: it cannot refund, transfer or arrange anything itself.
FALSE_ACTIONS = re.compile(r"\b(i(?:['’]ve| have) (?:issued|arranged|processed)|"
                           r"refund (?:has been|is being) (?:issued|processed)|transferr(?:ed|ing) you)", re.I)
# Saying a case was opened is true only when Salesforce confirmed a new or reopened case this turn.
CASE_OPENED = re.compile(r"\b(?:i|we)(?:['’]ve| have)\s+(?:now\s+)?(?:opened|created|raised|logged|reopened)\b", re.I)


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
    # Phase 4: the mandatory stage supplies an authorized order reference index;
    # the Supervisor can ask which purchase before a detail/eligibility check.
    Scenario("several_orders_needs_info", "C010", "Hi, I want to return something I bought.", None,
             ("tool.list_orders",), (r"helmet|gloves",), outcome="needs_information"),
    # Since Phase 3 the complaint stage may already have verified the delivery, so delegating is optional.
    Scenario("late_delivery", "C003", "My cooking pot was supposed to arrive already. Where is it?", None,
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
             ("tool.get_order",), (r"can(?:['’]|no)t|cannot|not (?:be )?returnable|(?:are|is)n['’]t returnable|unable",), outcome="done"),
    Scenario("faq_delivery_cost", "C001", "How much does delivery cost?", False, ("tool.get_knowledge",),
             (r"3\.99",), reply_not=(r"2\.99",)),
    Scenario("return_policy_general", "C001", "How many days do I have to send something back?", False,
             ("tool.get_knowledge",), (r"\b30\b|thirty",)),
    # Phase 3: a refund request becomes a Returns & Refunds staff case (checked below).
    Scenario("refund_request", "C002", "I want a refund for my Bluetooth speaker.", None, (),
             (r"can(?:['’]|no)t|cannot|unable|not able", r"0000\d{4}")),
    Scenario("other_customers_order", "C001", "What's the status of order SE-1051?", True, (),
             (r"(?:not|isn['’]t) (?:on|in|linked to|associated with) your account|only (?:help|see|check)",),
             outcome="cannot_complete"),
    Scenario("ask_for_human", "C001", "Can I speak to a real person, please?", False, (),
             (r"can(?:['’]|no)t|cannot|unable|not able",)),
]


@pytest.fixture(scope="module")
def loop():
    """One event loop for every scenario: the OpenAI client caches its HTTP pool across calls."""
    with asyncio.Runner() as runner:
        yield runner


@pytest.fixture(scope="module")
def live():
    try:
        return live_models(ModelSettings()), SalesforceSettings()
    except ValidationError:
        pytest.skip("set NABD_OPENAI_API_KEY and the Salesforce settings in backend/.env.nabd")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_english_support_scenario(scenario, live, loop):
    models, salesforce = live
    conversation = LiveConversation(scenario.customer, models, salesforce)
    result = loop.run(conversation.say(scenario.say))
    reply = result.reply
    completed = [e for e in result.events if e.type == "activity" and e.state == "completed"]
    codes = [e.code for e in completed]
    operations = [c.operation for c in result.commands]
    print(f"\n[{scenario.name}] {scenario.customer}: {scenario.say}\n  activity: {codes}\n"
          f"  case commands: {operations} confirmed {result.confirmed}\n  reply: {reply}")

    if scenario.orders is not None:
        assert ("agent.orders" in codes) is scenario.orders, codes
    for required in scenario.must_read:
        assert set(required.split("|")) & set(codes), f"{required} did not complete: {codes}"
    if scenario.outcome and scenario.orders:
        outcome = next(e.payload["result"]["outcome"] for e in completed if e.code == "agent.orders")
        assert outcome == scenario.outcome
    for pattern in scenario.reply:
        assert re.search(pattern, reply, re.I), f"reply misses /{pattern}/: {reply}"
    for pattern in scenario.reply_not:
        assert not re.search(pattern, reply, re.I), f"reply contains /{pattern}/: {reply}"
    assert not FALSE_ACTIONS.search(reply), f"reply claims an action it cannot take: {reply}"
    if CASE_OPENED.search(reply):
        assert any(op in ("create_case", "reopen_case") for op in operations) and result.confirmed, reply
    known = {case.number for case in conversation.world.customer_cases(scenario.customer)}
    assert set(CASE_NUMBER.findall(reply)) <= known, f"reply names a case that does not exist: {reply}"
    if scenario.name == "refund_request":
        [create] = [c for c in result.commands if c.operation == "create_case"]
        assert (create.category, create.staff_action) == ("Returns_Refunds", "refund")
    assert not any(e.state == "failed" and e.code.startswith("agent.") for e in result.events if e.code)
