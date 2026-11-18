"""Free invariants: language metadata, exact evidence, retry and case rules."""
import json
import pytest
from fakes import FakeChatModel, say
from multilingual_examples import FORMS
from nabd.agents.complaints import Assessment, ChatLine, verify_assessment
from nabd.agents.language import original_script
from nabd_fakes import issue
from test_complaints import AT, MID, World, scripted, turn

def answer(form, *issues, labels=()):
    return say(json.dumps({"outcome": "issues" if issues else "no_complaint", "question": None,
                           "issues": list(issues), "language": form.selection,
                           "message_languages": list(labels)}, ensure_ascii=False))

@pytest.mark.parametrize("form", FORMS, ids=lambda f: f.name)
def test_each_form_publishes_language_before_case_and_preserves_evidence(form):
    world = World()
    previous = ChatLine("customer", "previous-message", "Earlier English wording.", AT)
    model = scripted(answer(form, issue(language=form.language, customer_message_ids=[previous.message_id, MID]),
                            labels=[{"message_id": previous.message_id, "language": "en"},
                                    {"message_id": "not-in-this-conversation", "language": "fr"}]))
    reply, sink = turn(world, form.delayed, model, customer="C003", history=(previous,))
    [command] = world.gateway.calls
    assert command.category == "Delivery" and command.priority == "Low"
    evidence = {item.ref: item for item in command.evidence}
    assert (evidence[MID].text, evidence[MID].language, evidence[MID].script) == (
        form.delayed, form.language, original_script(form.delayed))
    assert (evidence[previous.message_id].text, evidence[previous.message_id].language,
            evidence[previous.message_id].script) == (previous.text, "en", "latin")
    stored = world.store.plans["run-1"]["assessment"]
    assert stored["language"] == form.selection and Assessment.from_json(stored).to_json() == stored
    assert "not-in-this-conversation" not in stored["message_languages"]
    language_at = next(i for i, e in enumerate(sink.events) if e.type == "conversation.language")
    case_at = next(i for i, e in enumerate(sink.events) if (e.code or "").startswith("case."))
    assert language_at < case_at and sink.events[language_at].payload == form.selection
    assert "Opened case 00002001" in reply

@pytest.mark.parametrize("form", FORMS, ids=lambda f: f.name)
def test_non_complaints_publish_language_without_creating_cases(form):
    world = World()
    _, sink = turn(world, form.knowledge, scripted(answer(form)), customer="C001")
    assert not world.gateway.calls
    assert [e.payload for e in sink.events if e.type == "conversation.language"] == [form.selection]

def test_language_survives_crash_without_reclassification_or_duplicate_case():
    world, form = World(), FORMS[4]
    original = world.store.mark_command
    async def crash(operation_id, status, **kwargs):
        if status == "confirmed" and not getattr(crash, "done", False):
            crash.done = True
            raise RuntimeError("interrupted after receipt")
        return await original(operation_id, status, **kwargs)
    world.store.mark_command = crash
    with pytest.raises(RuntimeError):
        turn(world, form.delayed, scripted(answer(form, issue(language="ary", customer_message_ids=[MID]))), customer="C003")
    unused = FakeChatModel()
    _, sink = turn(world, form.delayed, unused, customer="C003", attempt=2)
    assert not unused.requests and len(world.gateway.calls) == 1
    assert [e.payload for e in sink.events if e.type == "conversation.language"] == [form.selection]
    assert world.gateway.calls[0].evidence[0].text == form.delayed

def test_invalid_language_is_rejected_without_a_case():
    payload = {"outcome": "issues", "question": None, "issues": [issue()],
               "language": {"input_language": "fr", "reply_language": "unknown", "reply_script": "latin"}}
    result = verify_assessment(json.dumps(payload), evidence=set(), orders_seen=set(), candidates={},
                               customer_messages={}, current_message_id=MID)
    assert result.outcome == "needs_clarification" and not result.issues and result.language is None

@pytest.mark.parametrize("text,script", [("3afak rjje3 li flousi", "latin"), ("شكراً", "arabic"),
                                        ("شكراً SE-1061", "mixed"), ("£30 123 😊", None)])
def test_original_script_inspects_writing_system_not_intent(text, script):
    assert original_script(text) == script


def test_supervisor_gets_selected_language_and_orders_gets_exact_original_wording():
    import re
    from fakes import call
    from nabd_fakes import findings
    form = FORMS[3]
    supervisor = scripted(call("ask_orders_specialist", request="Check the speaker's return eligibility."), say("Wakha."))
    orders = scripted(findings(outcome="needs_information", questions=["Which order?"]))
    # Use the free support harness to inspect the exact payload crossing the agent boundary.
    from test_agents import run_turn
    import asyncio
    asyncio.run(run_turn(form.allowed_return, supervisor, orders, customer="C002"))
    request = orders.requests[0]["messages"][1].content
    original = json.loads(request.split("Original customer wording: ", 1)[1])
    assert original["current_customer_message"] == form.allowed_return
    # Stored JSONB may reorder keys; semantics, never textual order, choose language.
    from nabd.agents.supervisor import Supervisor, Turn
    from nabd.salesforce import fixtures as fx
    selected = {"reply_script": "arabic", "input_language": "ary", "reply_language": "ary"}
    prompt = Supervisor(FakeChatModel(), FakeChatModel(), None).system_prompt(
        Turn(MID, form.delayed, "Sara", fx.SEED_DATE, []), {"language": selected})
    embedded = re.search(r"selected language/script:\s*(\{[^}]+\})", prompt).group(1)
    assert json.loads(embedded) == selected


def test_darija_script_follows_original_unless_an_explicit_preference_overrides_it():
    from nabd.agents.language import TurnLanguage
    inferred = TurnLanguage(input_language="ary", reply_language="ary", reply_script="latin")
    assert inferred.public("arabic")["reply_script"] == "arabic"
    requested = TurnLanguage(input_language="ary", reply_language="ary", reply_script="latin", script_requested=True)
    assert requested.public("arabic")["reply_script"] == "latin"
    assert inferred.public("mixed")["reply_script"] == "latin"  # mixed/neutral text remains the model's choice


def test_complaint_gets_owned_order_context_before_deciding_to_ask_for_a_number():
    world = World()
    model = scripted(answer(FORMS[0], issue(category="Returns_Refunds", staff_action="refund",
                                          order_numbers=["SE-1051"], customer_message_ids=[MID])))
    _, sink = turn(world, FORMS[0].refund, model, customer="C002")
    request = model.requests[0]["messages"][1].content
    overview = json.loads(request.split("The customer's authorized order overview (list_orders):\n", 1)[1])
    assert {row["order_number"] for row in overview["orders"]} == {"SE-1051", "SE-1052", "SE-1053"}
    assert world.gateway.calls[0].order_number == "SE-1051"
    assert world.gateway.calls[0].category == "Returns_Refunds"
    assert any(e.code == "tool.list_orders" and e.state == "completed" for e in sink.events)


def test_latin_script_check_rewrites_once_without_exposing_draft_or_repeating_case_work():
    form, world = FORMS[3], World()
    supervisor = scripted(say("Ttsjjel case 00002001, شكراً."), say("Ttsjjel case 00002001, chokran."))
    reply, sink = turn(world, form.refund,
                       scripted(answer(form, issue(category="Returns_Refunds", staff_action="refund",
                                                   customer_message_ids=[MID]))),
                       customer="C002", supervisor=supervisor)
    assert reply == "Ttsjjel case 00002001, chokran."
    assert len(supervisor.requests) == 2 and supervisor.requests[1]["tool_choice"] == "none"
    streamed = "".join(e.payload["text"] for e in sink.events if e.type == "message.delta")
    assert streamed == reply and len(world.gateway.calls) == 1


def test_latin_script_check_is_bounded_and_fails_instead_of_publishing_invalid_text():
    form, world = FORMS[3], World()
    supervisor = scripted(say("شكراً"), say("شكراً"))
    with pytest.raises(RuntimeError, match="reply_script_invalid"):
        turn(world, form.knowledge, scripted(answer(form)), customer="C001", supervisor=supervisor)
    assert len(supervisor.requests) == 2 and not world.gateway.calls


def test_a_failed_script_rewrite_resumes_without_repeating_confirmed_case_work():
    form, world = FORMS[3], World()
    with pytest.raises(RuntimeError, match="reply_script_invalid"):
        turn(world, form.refund,
             scripted(answer(form, issue(category="Returns_Refunds", staff_action="refund", customer_message_ids=[MID]))),
             customer="C002", supervisor=scripted(say("شكراً"), say("شكراً")))
    unused = FakeChatModel()
    reply, _ = turn(world, form.refund, unused, customer="C002", attempt=2,
                    supervisor=scripted(say("Ttsjjel case 00002001.")))
    assert reply == "Ttsjjel case 00002001." and len(world.gateway.calls) == 1 and not unused.requests
