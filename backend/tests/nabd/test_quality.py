"""Offline evaluation validation and durable review/repair against PostgreSQL and MCP."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from fakes import FakeChatModel, call, say
from nabd.agents.quality import EnglishSummary, Evaluation, evaluate, valid_correction, validate_grade
from nabd.app import _production_chat
from nabd.context import mint
from nabd.database import open_database
from nabd.runtime.events import RunContext, TurnBudget
from nabd.runtime.quality_cli import _report
from nabd.runtime.quality_store import QualityStore
from nabd.runtime.quality_worker import QualityWorker
from nabd.runtime.store import NewCommand, Store
from nabd.runtime.worker import assistant_message_id
from nabd.salesforce.commands import CaseCommand
from nabd.settings import QualitySettings
from nabd_fakes import FakeGateway, FixtureReads, in_memory_write_connector, write_settings, WRITE_SECRET
from test_cases_postgres import _conversation, database  # noqa: F401
from test_postgres import empty_database  # noqa: F401

OLD = "The customer reports a broken kettle. We transferred GBP 999 to the customer."
UNSUPPORTED = "We transferred GBP 999 to the customer."
CORRECTED = "The customer reports that the kettle arrived broken."
CUST = "My kettle arrived broken."
SOURCE = "quality-test-source"
BASE = {"coverage": "pass", "language": "pass", "tool_use": "not_needed",
        "resolution_claims": "none", "grounding": "pass", "findings": [], "summaries": []}


def test_code_checks_quotes_scope_language_and_case_references():
    data = {"reply": "Your case 00009999 is open.", "language": {"reply_script": "arabic"},
            "known_case_numbers": [], "partial_trace": False,
            "confirmed_cases": [{"source_command": SOURCE, "case_number": "00002001", "summary": OLD,
                                 "evidence": [{"text": CUST}]}]}
    output = Evaluation.model_validate({**BASE,
        "findings": [{"quote": "invented quote", "check": "coverage", "severity": "low", "reason": "bogus"}],
        "summaries": [
            {"source_command": "some-other-case", "verdict": "needs_correction",
             "unsupported": [UNSUPPORTED], "missing": []},
            {"source_command": SOURCE, "verdict": "needs_correction",
             "unsupported": [UNSUPPORTED, "invented"], "missing": ["private notes"]},
        ]})
    grade = validate_grade(output, data)
    assert grade["dropped"] == 4
    assert grade["findings"] == []
    assert grade["grades"]["language"] == grade["grades"]["grounding"] == "fail"
    assert grade["grades"]["coverage"] == "partial"
    assert {item["code"] for item in grade["checks"]} == {"script_mismatch", "missing_confirmed_case", "unknown_case"}
    assert grade["summaries"][0]["unsupported"] == [UNSUPPORTED]


@pytest.mark.parametrize("text", ["SE-9901 was refunded.", "The customer paid £5.", "Refunded on 20 November.",
                                 "The package arrived on 19/11.", "وصل الطرد", OLD, UNSUPPORTED.lower()])
def test_corrections_reject_new_references_wrong_script_or_surviving_claims(text):
    evidence = [{"text": CUST + " I bought 5 items in November."}]
    assert not valid_correction(EnglishSummary(language="en", summary=text), OLD, evidence, [UNSUPPORTED])
    assert valid_correction(EnglishSummary(language="en", summary=CORRECTED), OLD, evidence, [UNSUPPORTED])


def test_evaluator_cannot_supply_tool_calls():
    model = FakeChatModel(replies=[call("revise_case_summary", text=json.dumps(BASE))])
    with pytest.raises(ValueError, match="tool call"):
        asyncio.run(evaluate(model, {}))
    assert model.requests[0]["tools"] == [] and model.requests[0]["tool_choice"] == "none"


def test_release_campaign_uses_one_async_runner_across_all_paid_modules():
    selections = [
        "tests/nabd/test_multilingual_scenarios.py::test_multilingual_scenario[mixed-knowledge]",
        "tests/nabd/test_release_challenges.py::test_release_challenge[english-injection]",
        "tests/nabd/test_release_evaluator.py::test_labeled_evaluator[good_clarification]",
    ]
    result = subprocess.run([sys.executable, "-m", "pytest", "--setup-plan", "-m", "llm", *selections],
                            cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, check=True)
    assert len(re.findall(r"SETUP\s+S\s+release_loop\b", result.stdout)) == 1, result.stdout
    assert len(re.findall(r"TEARDOWN\s+S\s+release_loop\b", result.stdout)) == 1, result.stdout


def test_invalid_quality_environment_leaves_production_chat_configured(monkeypatch):
    monkeypatch.setenv("NABD_READ_MCP_CONTEXT_SECRET", "fixture-read-credential-long-enough")
    monkeypatch.setenv("NABD_WRITE_MCP_CONTEXT_SECRET", "fixture-write-credential-long-enough")
    monkeypatch.setenv("NABD_OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("NABD_SALESFORCE_DOMAIN_URL", "https://fixture.my.salesforce.com")
    monkeypatch.setenv("NABD_SALESFORCE_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("NABD_SALESFORCE_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv("NABD_QUALITY_LEASE_SECONDS", "invalid")

    async def scenario():
        async with AsyncExitStack() as stack:
            chat = await _production_chat(stack)
            assert chat is not None and chat.evaluator is None

    asyncio.run(scenario())


async def _queued(db, *, review=True, source=False):
    store = Store(db.pool)
    convo, [(run_id, _)] = await _conversation(store, "C003", [CUST])
    run = await store.claim_run("test-worker", 5, 3)
    private = "authorized tool result only"
    access = mint(WRITE_SECRET, customer_id="C003", conversation_id=convo, run_id=run_id, ttl_seconds=60)
    trace = RunContext(convo, run_id, 2, "C003", store, TurnBudget(0))
    trace.record("supervisor", "tool", "get_order", arguments={"order_number": "SE-1061"},
                 result={"details": private, "credentials": "configured-secret-sentinel",
                         "extra": access})
    gateway = FakeGateway(FixtureReads())
    if source:
        evidence = [{"ref": "quality-msg", "kind": "customer_report", "text": CUST}]
        command = CaseCommand(operation_id=SOURCE, operation="create_case", customer_id="C003",
                              issue_id="quality-test-issue", category="Orders_Products", priority="Low",
                              subject="Broken kettle", summary=OLD, evidence=evidence)
        result = await gateway.execute(command)
        await store.add_command(run_id, convo, "C003",
                                NewCommand(SOURCE, "0", "issue", "create_case", {"summary": OLD, "evidence": evidence}))
        await store.mark_command(SOURCE, "confirmed", result=result.model_dump())
        reply = f"We opened case {result.case_number} about the broken kettle."
    else:
        reply = "How can I help?"
    assert await store.complete_run(run, assistant_message_id(run_id), reply, trace=trace, review=review)
    assert not await store.complete_run(run, assistant_message_id(run_id), reply, trace=trace, review=review)
    return store, convo, run_id, gateway, private, access


def _worker(store, gateway, *, grading=BASE, wording=CORRECTED, **settings):
    quality = QualitySettings(_env_file=None, max_attempts=2, **settings)
    evaluator = FakeChatModel(default=say(json.dumps(grading) if isinstance(grading, dict) else grading))
    corrector = FakeChatModel(default=say(json.dumps({"language": "en", "summary": wording})))
    return QualityWorker(QualityStore(store), evaluator, corrector, in_memory_write_connector(gateway),
                         write_settings(), quality, 12)


@pytest.mark.postgres
def test_review_reclaims_expired_lease_and_rejects_stale_grade(database):
    async def scenario():
        async with open_database(database) as db:
            store, convo, run_id, gateway, private, access = await _queued(db)
            quality = QualityStore(store)
            stale = await quality.claim("quality_reviews", lease=5, max_attempts=2)
            assert await quality.claim("quality_reviews", lease=5, max_attempts=2) is None
            async with db.pool.connection() as conn:
                await conn.execute("UPDATE nabd.quality_reviews SET lease_until = now()")
            worker = _worker(store, gateway)
            assert await worker.process_one("quality_reviews")
            assert not await quality.grade(stale, {"bad": True}, {}, [])
            assert not await worker.process_one("quality_reviews")
            report = await _report(quality, None, worker.settings)
            assert report["overall"]["counts"] == {"pass": 1}
            assert report["tokens_by_agent"]["quality"]["total"]["input"] == 5
            assert report["turn_ms"]["samples"] == 1
            [item] = await quality.trace(run_id)
            assert item["partial"] and item["result"]["details"] == private
            assert item["result"]["extra"] == item["result"]["credentials"] == "[redacted]"
            public = json.dumps(await store.snapshot(convo))
            assert private not in public and access not in public and "quality_reviews" not in public
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("enabled", [True, False])
def test_invalid_output_retries_to_failure_and_disabled_does_not_enqueue(database, enabled):
    async def scenario():
        async with open_database(database) as db:
            store, _, _, gateway, _, _ = await _queued(db, review=enabled)
            worker = _worker(store, gateway, grading="invalid JSON", enabled=enabled)
            assert await worker.process_one("quality_reviews") is enabled
            async with db.pool.connection() as conn:
                await conn.execute("UPDATE nabd.quality_reviews SET not_before = now()")
            assert await worker.process_one("quality_reviews") is enabled
            assert not await worker.process_one("quality_reviews")
            report = await _report(worker.store, None, worker.settings)
            assert report["review_jobs"] == ({"failed": 1} if enabled else {"absent": 1})
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("path", ["unclear", "version_changed", "staff_override", "invalid", "already_dispatched"])
def test_summary_revision_is_one_fenced_private_operation(database, path):
    async def scenario():
        async with open_database(database) as db:
            store, convo, _, gateway, private, _ = await _queued(db, source=True)
            grading = {**BASE, "summaries": [{"source_command": SOURCE, "verdict": "needs_correction",
                                             "unsupported": [UNSUPPORTED], "missing": []}]}
            worker = _worker(store, gateway, grading=grading, wording=OLD if path == "invalid" else CORRECTED)
            events = await store.events_after(convo, 0)
            assert await worker.process_one("quality_reviews")
            assert not await worker.process_one("quality_reviews")
            case = next(c for c in gateway.reads.customer_cases("C003") if c.issue_id == "quality-test-issue")
            if path == "unclear":
                gateway.fail = ["after"]
            elif path == "version_changed":
                gateway.reads.touch(case)
            elif path == "staff_override":
                case.summary = "Staff wording."  # simulate a same-version edit to exercise the hash guard
            elif path == "already_dispatched":
                job = await worker.store.claim("summary_revisions", lease=5, max_attempts=2)
                source = await worker.store.revision_context(job)
                args = {"operation_id": job["operation_id"], "case_id": case.id,
                        "expected_version": source["result"]["case_version"], "summary": CORRECTED}
                assert await worker.store.prepare(job, args, {})
                assert await worker.store.dispatch(job)
                await gateway.execute(CaseCommand(operation="revise_summary", customer_id="C003", **args))
                async with db.pool.connection() as conn:
                    await conn.execute("UPDATE nabd.summary_revisions SET lease_until = now()")
            assert await worker.process_one("summary_revisions")
            assert not await worker.process_one("summary_revisions")
            async with db.pool.connection() as conn:
                [job] = await (await conn.execute("SELECT * FROM nabd.summary_revisions")).fetchall()
            if path in ("unclear", "already_dispatched"):
                assert job["status"] == "confirmed" and case.summary == CORRECTED
                assert job["result"]["replayed"] is True
            elif path == "invalid":
                assert job["status"] == "rejected" and job["request"] is None and case.summary == OLD
            else:
                assert job["status"] == "conflict" and job["error_code"] == path
            assert len(gateway.calls) == (1 if path == "invalid" else 2)
            assert await store.events_after(convo, 0) == events
            public = json.dumps(await store.snapshot(convo))
            assert not any(secret in public for secret in (private, OLD, CORRECTED, "needs_correction"))
            if path == "already_dispatched":
                assert not worker.complaints.requests
    asyncio.run(scenario())
