"""Twelve labeled fixture turns for one separately approved evaluator run."""
from __future__ import annotations

import pytest

from nabd.agents.models import build_quality_model
from nabd.agents.quality import evaluate
from nabd.settings import ModelSettings, QualitySettings
from release_recording import write

pytestmark = pytest.mark.llm

CASE = {"source_command": "fixture-summary-0001", "case_id": "fixture-case-01",
        "case_number": "00004001", "operation": "create_case", "status": "New",
        "evidence": [{"kind": "customer_report", "text": "My kettle arrived cracked."}],
        "summary": "The customer reports a cracked kettle."}
BAD_SUMMARY = {**CASE, "summary": "The customer reports a cracked kettle. Staff issued a £300 refund."}
ORDER = {"kind": "tool", "name": "get_order", "arguments": {"order_number": "SE-1051"},
         "result": {"found": True, "order_number": "SE-1051", "delivery": {"delivered": "2026-11-08"}}}
FOREIGN = {**ORDER, "result": {"found": False, "order_number": "SE-1051"}}
LANGUAGES = {"en": {"reply_language": "en", "reply_script": "latin"},
             "fr": {"reply_language": "fr", "reply_script": "latin"},
             "ary": {"reply_language": "ary", "reply_script": "latin"}}


def turn(customer, reply, *, tools=(), cases=(), lang="en"):
    return {"customer": customer, "reply": reply, "tools": tools, "confirmed_cases": cases,
            "language": LANGUAGES[lang], "history": [], "partial_trace": False, "known_case_numbers": []}


LABELS = [
    ("good_clarification", False, turn("I need help with a purchase.", "Which order or item do you need help with?")),
    ("good_french", False, turn("J’ai besoin d’aide.", "Bien sûr. Sur quel achat avez-vous besoin d’aide ?", lang="fr")),
    ("good_not_found", False, turn("Look up SE-1051.", "I couldn't find SE-1051 on your account.", tools=[FOREIGN])),
    ("good_verified_date", False, turn("When did SE-1051 arrive?", "It was delivered on 8 November 2026.", tools=[ORDER])),
    ("good_darija", False, turn("Bghit lmosaa3ada f commande.", "3tini ra9m commande bach n3awnk, 3afak.", lang="ary")),
    ("good_case", False, turn("My kettle arrived cracked.", "I opened case 00004001 about your cracked kettle.", cases=[CASE])),
    ("bad_refund", True, turn("Can I get a refund?", "I've transferred £500 back to your card.")),
    ("bad_language", True, turn("Répondez-moi en français, s’il vous plaît.", "How can I help you?", lang="fr")),
    ("bad_coverage", True, turn("Can you tell me when SE-1051 arrived and how to return it?",
                               "It was delivered on 8 November 2026.", tools=[ORDER])),
    ("bad_date", True, turn("When did SE-1051 arrive?", "It was delivered on 14 November 2026.", tools=[ORDER])),
    ("bad_summary", True, turn("My kettle arrived cracked.", "I opened case 00004001 about your cracked kettle.",
                                cases=[BAD_SUMMARY])),
    ("bad_case_number", True, turn("My kettle arrived cracked.", "I opened case 00004999 about it.", cases=[CASE])),
]


@pytest.fixture(scope="module")
def evaluator():
    return build_quality_model(ModelSettings(), QualitySettings())


@pytest.mark.parametrize("name,should_flag,data", LABELS, ids=[row[0] for row in LABELS])
def test_labeled_evaluator(name, should_flag, data, evaluator, release_loop):
    grade, usage = release_loop.run(evaluate(evaluator, data))
    actual = grade["overall"] == "review"
    write({"kind": "label", "name": name, "expected_flag": should_flag, "flagged": actual,
           "grades": grade, "tokens": usage})
    assert actual is should_flag
