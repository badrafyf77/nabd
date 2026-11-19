"""Executing runs: the turn executor, the worker loop and the event notifier.

The worker lives in the API process. Each slot claims a run, renews its lease
while the turn executes, and records the outcome. A crash leaves the lease to
expire; any worker then claims the run again and the Supervisor resumes from
its checkpoint. The turn deadline fails a run without retrying it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass

from psycopg import AsyncConnection

from nabd.agents.cases import ComplaintStage
from nabd.agents.complaints import ChatLine
from nabd.agents.mcp_tools import McpConnector
from nabd.agents.supervisor import Supervisor, Turn, assistant_message_id
from nabd.context import CASE_HISTORY_SCOPE, READ_SCOPE, WRITE_AUDIENCE, WRITE_SCOPE, mint
from nabd.runtime.events import NewEvent, RunContext, TurnBudget
from nabd.runtime.store import NOTIFY_CHANNEL, ClaimedRun, CommandRow, Store
from nabd.settings import BusinessSettings, ChatSettings, ReadMcpSettings, WriteMcpSettings

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0


class EventNotifier:
    """Wakes SSE streams when a conversation gets new events, from any process."""

    def __init__(self, conninfo: str | None = None):
        self.conninfo = conninfo
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    def wake(self, conversation_id: str) -> None:
        for event in list(self._waiters.get(conversation_id, ())):
            event.set()

    @contextlib.contextmanager
    def subscribe(self, conversation_id: str):
        event = asyncio.Event()
        self._waiters[conversation_id].add(event)
        try:
            yield event
        finally:
            self._waiters[conversation_id].discard(event)
            if not self._waiters[conversation_id]:
                self._waiters.pop(conversation_id, None)

    async def listen(self) -> None:
        """Forward PostgreSQL notifications; reconnect after failures. SSE also polls."""
        while self.conninfo:
            try:
                async with await AsyncConnection.connect(self.conninfo, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    async for notify in conn.notifies():
                        self.wake(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # never log the connection string
                logger.warning("Event listener disconnected (%s); retrying", type(error).__name__)
                await asyncio.sleep(2)


@dataclass
class TurnExecutor:
    supervisor: Supervisor
    connect: McpConnector
    read_mcp: ReadMcpSettings
    chat: ChatSettings
    business: BusinessSettings
    complaints: ComplaintStage
    connect_writes: McpConnector
    write_mcp: WriteMcpSettings
    quality_enabled: bool = True

    def _tokens(self, customer_id: str, conversation_id: str, run_id: str) -> tuple[str, str, str]:
        """Read (Supervisor and Orders), case-history read (Complaint stage) and write tokens of one run."""
        ttl = self.chat.turn_deadline_seconds + 60
        common = {"customer_id": customer_id, "conversation_id": conversation_id, "run_id": run_id,
                  "ttl_seconds": ttl}
        return (
            mint(self.read_mcp.context_secret, **common),
            mint(self.read_mcp.context_secret, scopes=(READ_SCOPE, CASE_HISTORY_SCOPE), **common),
            mint(self.write_mcp.context_secret, scopes=(WRITE_SCOPE,), audience=WRITE_AUDIENCE, **common),
        )

    async def execute(self, store: Store, run: ClaimedRun) -> None:
        context = RunContext(run.conversation_id, run.id, run.attempt, run.customer_id, store,
                             TurnBudget(self.chat.turn_tool_calls), self.chat.agent_tool_calls)
        if run.attempt > 1:
            await context.emit(NewEvent(type="run.retrying", state="retrying", payload={"attempt": run.attempt}))
        await context.emit(NewEvent(type="run.started", state="running", payload={"attempt": run.attempt}))
        read, case_read, write = self._tokens(run.customer_id, run.conversation_id, run.id)
        async with asyncio.timeout(self.chat.turn_deadline_seconds):
            async with self.connect(read) as tools, self.connect(case_read) as case_reads, \
                    self.connect_writes(write) as writes:
                index = (await tools.read_json("nabd://knowledge/index"))["entries"]
                lines = await store.recent_messages(run.conversation_id, run.message_id, self.chat.history_messages)
                staff = await store.staff_history(run.conversation_id)
                lines += [{"role": "staff", "id": line["id"],
                           "text": f"Case {line['case_number']}: {line['text']}", "at": line["at"]} for line in staff]
                lines.sort(key=lambda line: line["at"])
                history = tuple(ChatLine(line["role"], line["id"], line["text"], line["at"]) for line in lines)
                at = next((line.at for line in history if line.message_id == run.message_id), None)
                turn = Turn(run.message_id, run.text, run.customer_name, self.business.today(), index, history, at)

                async def stage() -> dict:
                    return await self.complaints.run(context, turn, case_reads, writes)

                text = await self.supervisor.run_turn(context, tools, turn, stage=stage)
        if not text.strip():
            raise RuntimeError("empty_reply")  # localized failure card; never inject an English fallback
        await store.complete_run(run, assistant_message_id(run.id), text, trace=context, review=self.quality_enabled)

    async def reconcile(self, store: Store, row: CommandRow) -> None:
        """Settle one case command whose run ended before Salesforce gave a definite answer."""
        _, _, write = self._tokens(row.customer_id, row.conversation_id, row.run_id)
        context = RunContext(row.conversation_id, row.run_id, 0, row.customer_id, store, TurnBudget(0))
        async with store.case_lock(row.customer_id), self.connect_writes(write) as writes:
            await self.complaints.reconcile(context, row, writes)

    async def give_up(self, store: Store, row: CommandRow) -> None:
        """Close the card of a command that was abandoned or used up its attempts."""
        context = RunContext(row.conversation_id, row.run_id, 0, row.customer_id, store, TurnBudget(0))
        activity = self.complaints.card(context, row)
        await activity.fail(row.error_code or "failed")


class RunWorker:
    def __init__(self, store: Store, executor: TurnExecutor, settings: ChatSettings):
        self.store = store
        self.executor = executor
        self.settings = settings
        self.worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        self._wakeup = asyncio.Event()

    def wake(self, *_: object) -> None:
        self._wakeup.set()

    async def run_forever(self) -> None:
        async with asyncio.TaskGroup() as group:
            for slot in range(self.settings.worker_concurrency):
                group.create_task(self._slot(slot))
            group.create_task(self._reconcile_forever())

    async def reconcile_once(self) -> int:
        """Settle leftover case commands of finished runs; returns how many were handled."""
        rows, given_up = await self.store.claim_reconcile(
            older_than_seconds=self.settings.reconcile_after_seconds, lease_seconds=60,
            max_attempts=self.settings.max_reconcile_attempts)
        for row in given_up:
            with contextlib.suppress(Exception):
                await self.executor.give_up(self.store, row)
        for row in rows:
            try:
                await self.executor.reconcile(self.store, row)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Reconciling case command %s failed (%s)", row.operation_id, type(error).__name__)
        return len(rows) + len(given_up)

    async def _reconcile_forever(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Case reconciliation pass failed (%s)", type(error).__name__)
            await asyncio.sleep(self.settings.reconcile_interval_seconds)

    async def _slot(self, slot: int) -> None:
        while True:
            try:
                run = await self.store.claim_run(self.worker_id, self.settings.run_lease_seconds,
                                                 self.settings.max_run_attempts)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Claiming a run failed (%s)", type(error).__name__)
                run = None
            if run is None:
                self._wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), POLL_SECONDS)
                continue
            await self.process(run)

    async def process(self, run: ClaimedRun) -> None:
        """Execute one claimed run while keeping its lease; record success, retry or failure."""
        task = asyncio.create_task(self.executor.execute(self.store, run))
        lost = False
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.settings.run_lease_seconds / 3)
                if not done and not await self.store.renew_lease(run, self.settings.run_lease_seconds):
                    lost = True
                    task.cancel()
            await task
        except asyncio.CancelledError:
            if lost:
                logger.warning("Run %s lost its lease; another attempt will continue it", run.id)
                return
            raise
        except TimeoutError:
            logger.warning("Run %s exceeded the turn deadline", run.id)
            await self.store.fail_run(run, "deadline_exceeded")
        except Exception as error:
            logger.exception("Run %s attempt %s failed", run.id, run.attempt)
            code = type(error).__name__[:40]
            if run.attempt < self.settings.max_run_attempts:
                await self.store.release_run(run, code)
                self.wake()
            else:
                await self.store.fail_run(run, "failed")
