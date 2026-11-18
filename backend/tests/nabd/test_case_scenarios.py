"""Phase 3 English complaint and case scenarios with REAL models (paid).

Full turns as the worker runs them (complaint stage, then Supervisor) through the
real read and write MCP servers; orders and knowledge from the real Salesforce
org, cases from the fixture world (``live_turns``). Opt-in only:
``pytest -m llm tests/nabd/test_case_scenarios.py -s``. Ask before running: the
user pays for the OpenAI calls. Each scenario runs once.

Checks are deterministic where code decides: which case commands were sent, to
which case, with which category and staff action; and that the reply names only
cases that exist and claims an opened case only when one was confirmed.
"""
import asyncio
import re
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from live_turns import CASE_NUMBER, LiveConversation, live_models
from nabd.settings import ModelSettings, SalesforceSettings
from test_chat_scenarios import CASE_OPENED, FALSE_ACTIONS

pytestmark = pytest.mark.llm


@dataclass
class Step:
    say: str
    operations: tuple[str, ...] | None  # exact case commands expected this turn; None: see forbid
    case: str | None = None  # the existing case the command must target
    categories: tuple[str, ...] = ()  # categories of new cases
    staff_action: str | None = None
    forbid: tuple[str, ...] = ()
    reply: tuple[str, ...] = ()
    reply_not: tuple[str, ...] = ()


@dataclass
class CaseScenario:
    name: str
    customer: str
    steps: tuple[Step, ...]


SCENARIOS = [
    CaseScenario("delayed_delivery_opens_a_case", "C003", (
        Step("My cooking pot was supposed to arrive already. Where is it?", ("create_case",),
             categories=("Delivery",), reply=(r"20 November|Friday", r"0000\d{4}", r"Delivery")),)),
    CaseScenario("lost_parcel_updates_the_existing_case", "C004", (
        Step("My smart watch never arrived. It's been almost two weeks.", ("update_case",), case="00001001",
             reply=(r"00001001",)),)),
    CaseScenario("refund_request_becomes_a_staff_case", "C002", (
        Step("I want a refund for my Bluetooth speaker.", ("create_case",), categories=("Returns_Refunds",),
             staff_action="refund", reply=(r"can(?:['’]|no)t|cannot|unable|not able", r"0000\d{4}",
                                           r"Returns")),)),
    CaseScenario("two_problems_open_two_cases", "C003", (
        Step("My cooking pot is late, and the yoga mat from my other order arrived torn.",
             ("create_case", "create_case"), categories=("Delivery", "Orders_Products")),)),
    CaseScenario("a_question_needs_no_case", "C001", (
        Step("How much does delivery cost?", (), reply=(r"3\.99",)),)),
    CaseScenario("an_on_time_order_needs_no_case", "C001", (
        Step("Where is my order SE-1042?", (), reply=(r"transit|on (?:its|the) way|DPD|20 November",)),)),
    CaseScenario("thanks_is_not_a_closure", "C007", (
        Step("My order SE-1101 says it was delivered on Monday but I still can't find my plant pot.",
             ("update_case",), case="00001008"),
        Step("Ok, thanks.", None, forbid=("close_case", "request_closure", "create_case")),
    )),
    CaseScenario("a_confirmed_resolution_closes_the_case", "C007", (
        Step("Good news: I found the plant pot from SE-1101, my neighbour had taken it in. It's all sorted, "
             "you can close my case.", ("close_case",), case="00001008", reply=(r"clos",)),)),
]


@pytest.fixture(scope="module")
def loop():
    with asyncio.Runner() as runner:
        yield runner


@pytest.fixture(scope="module")
def live():
    try:
        return live_models(ModelSettings()), SalesforceSettings()
    except ValidationError:
        pytest.skip("set NABD_OPENAI_API_KEY and the Salesforce settings in backend/.env.nabd")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_english_case_scenario(scenario, live, loop):
    models, salesforce = live
    conversation = LiveConversation(scenario.customer, models, salesforce)
    for number, step in enumerate(scenario.steps, 1):
        result = loop.run(conversation.say(step.say))
        operations = [c.operation for c in result.commands]
        print(f"\n[{scenario.name} #{number}] {scenario.customer}: {step.say}\n  activity: {result.completed()}\n"
              f"  case commands: {operations} confirmed {result.confirmed}\n  reply: {result.reply}")
        if step.operations is not None:
            assert sorted(operations) == sorted(step.operations), operations
        assert not set(operations) & set(step.forbid), operations
        if step.case:
            target = conversation.world.case(step.case).id
            assert all(c.case_id == target for c in result.commands if c.operation != "create_case")
        creates = [c for c in result.commands if c.operation == "create_case"]
        assert sorted(c.category for c in creates) == sorted(step.categories)
        if step.staff_action:
            assert all(c.staff_action == step.staff_action for c in creates)
        assert len(result.confirmed) == len(result.commands), "every case command was confirmed"
        for pattern in step.reply:
            assert re.search(pattern, result.reply, re.I), f"reply misses /{pattern}/"
        for pattern in step.reply_not:
            assert not re.search(pattern, result.reply, re.I), f"reply contains /{pattern}/"
        assert not FALSE_ACTIONS.search(result.reply)
        if CASE_OPENED.search(result.reply):
            assert {"create_case", "reopen_case"} & set(operations), "claims an opened case none was confirmed"
        known = {case.number for case in conversation.world.customer_cases(scenario.customer)}
        assert set(CASE_NUMBER.findall(result.reply)) <= known, "names a case that does not exist"
        assert not any(e.state == "failed" and (e.code or "").startswith("agent.") for e in result.events)
