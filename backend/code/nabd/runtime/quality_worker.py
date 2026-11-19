"""Independent background processing. No customer events or chat mutations."""
from __future__ import annotations

import asyncio
import logging
import uuid

from langchain_core.language_models import BaseChatModel

from nabd.agents.cases import AMBIGUOUS
from nabd.agents.mcp_tools import McpConnector, McpTools
from nabd.agents.quality import correct_summary, evaluate
from nabd.context import WRITE_AUDIENCE, WRITE_SCOPE, mint
from nabd.runtime.quality_store import QualityStore, Queue
from nabd.salesforce.commands import CommandResult
from nabd.settings import QualitySettings, WriteMcpSettings

log = logging.getLogger(__name__)


class RetryQuality(Exception):
    """A transient or ambiguous MCP response."""


class QualityWorker:
    def __init__(self, store: QualityStore, evaluator: BaseChatModel, complaints: BaseChatModel,
                 connect: McpConnector, writes: WriteMcpSettings, settings: QualitySettings, history_limit: int):
        self.store = store
        self.evaluator = evaluator
        self.complaints = complaints
        self.connect = connect
        self.writes = writes
        self.settings = settings
        self.history_limit = history_limit

    async def run_forever(self) -> None:
        async with asyncio.TaskGroup() as workers:
            workers.create_task(self._poll("quality_reviews"))
            if self.settings.summary_correction_enabled:
                workers.create_task(self._poll("summary_revisions"))

    async def _poll(self, queue: Queue) -> None:
        while True:
            try:
                if await self.process_one(queue):
                    continue
            except Exception as exc:
                # Error details can include raw provider output or customer data.
                log.warning("NABD %s worker unavailable (%s)", queue, type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    async def process_one(self, queue: Queue) -> bool:
        if not self.settings.enabled or (queue == "summary_revisions" and not self.settings.summary_correction_enabled):
            return False
        job = await self.store.claim(queue, lease=self.settings.lease_seconds, max_attempts=self.settings.max_attempts)
        if job is None:
            return False
        try:
            async with asyncio.timeout(self.settings.lease_seconds - 1):
                if queue == "quality_reviews":
                    await self._grade(job)
                else:
                    await self._revise(job)
        except Exception as exc:
            await self.store.retry(queue, job, type(exc).__name__, self.settings.max_attempts)
        return True

    async def _grade(self, job: dict) -> None:
        data = await self.store.load_review(job["run_id"], self.history_limit)
        grade, usage = await evaluate(self.evaluator, data)
        revisions = []
        if self.settings.summary_correction_enabled:
            cases = {case["source_command"]: case for case in data["confirmed_cases"]}
            for review in grade["summaries"]:
                source = review["source_command"]
                case = cases[source]
                if review["verdict"] == "needs_correction" and case["case_id"]:
                    revisions.append({"source_command": source, "case_id": case["case_id"], "assessment": review,
                                      "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"nabd:summary:{source}"))})
        await self.store.grade(job, grade, usage, revisions)

    async def _revise(self, job: dict) -> None:
        source = await self.store.revision_context(job)
        if not source or source["result"].get("case_id") != job["case_id"] or not source["result"].get("case_version"):
            await self.store.finish_revision(job, "rejected", error="missing_case_version")
            return
        request = job["request"]
        if request is None:
            summary, usage = await correct_summary(self.complaints, source["request"], job["assessment"])
            if summary is None:
                await self.store.finish_revision(job, "rejected", error="invalid_correction", usage=usage)
                return
            request = {"operation_id": job["operation_id"], "case_id": job["case_id"],
                       "expected_version": source["result"]["case_version"], "summary": summary}
            if not await self.store.prepare(job, request, usage):
                return
        token = mint(self.writes.context_secret, customer_id=source["customer_id"],
                     conversation_id=str(source["conversation_id"]), run_id=str(source["run_id"]),
                     ttl_seconds=self.settings.lease_seconds + 10, audience=WRITE_AUDIENCE, scopes=(WRITE_SCOPE,))
        async with self.connect(token) as writes:
            result = await self._settle(job, request, writes)
        if result is not None:
            await self.store.finish_revision(job, result.status, error=result.reason,
                                             result=result.model_dump(mode="json"))

    async def _settle(self, job: dict, request: dict, writes: McpTools) -> CommandResult | None:
        if job["dispatched"]:
            known = await self._lookup(writes, job["operation_id"])
            if known.status != "unknown":
                return known
            if job["attempts"] > self.settings.max_attempts:
                return CommandResult(status="failed", reason="receipt_unknown")
        if not await self.store.dispatch(job):
            return None
        outcome = await writes.call_raw("revise_case_summary", request)
        if outcome.ok:
            result = CommandResult.model_validate(outcome.data)
            if result.status != "unknown":
                return result
        elif outcome.error not in AMBIGUOUS:
            return CommandResult(status="failed", reason=outcome.error)
        known = await self._lookup(writes, job["operation_id"])
        if known.status == "unknown":
            raise RetryQuality()
        return known

    @staticmethod
    async def _lookup(writes: McpTools, operation_id: str) -> CommandResult:
        outcome = await writes.call_raw("get_operation", {"operation_id": operation_id})
        if not outcome.ok:
            raise RetryQuality()
        return CommandResult.model_validate(outcome.data)
