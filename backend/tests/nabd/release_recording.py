"""Redacted metrics and fixture replies for one bounded campaign invocation."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from nabd.agents.models import build_quality_model
from nabd.settings import ModelSettings, QualitySettings


def write(record):
    if path := os.environ.get("NABD_CAMPAIGN_JSONL"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@pytest.fixture(scope="module")
def release_evaluator():
    if os.environ.get("NABD_CAMPAIGN_JSONL"):
        return build_quality_model(ModelSettings(), QualitySettings())
    return None


def record_turn(name, form, number, result, *, case_ok):
    if not os.environ.get("NABD_CAMPAIGN_JSONL"):
        return
    tokens = defaultdict(Counter)
    for item in result.trace:
        if item["kind"] == "model" and item["usage"]:
            tokens[item["agent"]].update(item["usage"])
    if result.quality_usage:
        tokens["quality"].update(result.quality_usage)
    # Raw fixture replies and grades are for the local review directory.
    # Tool data, customer profiles and prompts never enter campaign artifacts.
    write({"kind": "turn", "scenario": name, "form": form, "turn": number, "reply": result.reply,
           "case_ok": case_ok, "grade": result.grade, "quality_error": result.quality_error,
           "duration_ms": result.duration_ms, "first_text_ms": result.first_text_ms, "tokens": tokens})


def case_matches(result, conversation, expected):
    """Compare confirmed operations, categories, staff actions and target identity."""
    if expected is None:
        return None
    if len(result.commands) != len(expected) or len(result.confirmed) != len(expected):
        return False
    actual = []
    for command, number in zip(result.commands, result.confirmed, strict=True):
        case = conversation.world.case(number)
        actual.append((command.operation, case.category, command.staff_action, case.order_number, number))
    for match in expected:
        index = next((i for i, row in enumerate(actual) if all(want is None or want == got
                                                               for want, got in zip(match, row, strict=True))), None)
        if index is None:
            return False
        actual.pop(index)
    return True
