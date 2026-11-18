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
