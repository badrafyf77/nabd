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

from nabd.agents.mcp_tools import McpConnector
from nabd.agents.supervisor import Supervisor, Turn, assistant_message_id
from nabd.context import mint
from nabd.runtime.events import NewEvent, RunContext, TurnBudget
from nabd.runtime.store import NOTIFY_CHANNEL, ClaimedRun, Store
from nabd.settings import BusinessSettings, ChatSettings, ReadMcpSettings

logger = logging.getLogger(__name__)

EMPTY_REPLY = "Sorry, I couldn't put an answer together. Could you say that another way?"
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

    async def execute(self, store: Store, run: ClaimedRun) -> None:
        context = RunContext(run.conversation_id, run.id, run.attempt, run.customer_id, store,
                             TurnBudget(self.chat.turn_tool_calls), self.chat.agent_tool_calls)
        if run.attempt > 1:
            await context.emit(NewEvent(type="run.retrying", state="retrying", payload={"attempt": run.attempt}))
        await context.emit(NewEvent(type="run.started", state="running", payload={"attempt": run.attempt}))
        bearer = mint(self.read_mcp.context_secret, customer_id=run.customer_id,
                      conversation_id=run.conversation_id, run_id=run.id,
                      ttl_seconds=self.chat.turn_deadline_seconds + 60)
        async with asyncio.timeout(self.chat.turn_deadline_seconds):
            async with self.connect(bearer) as tools:
                index = (await tools.read_json("nabd://knowledge/index"))["entries"]
                turn = Turn(run.message_id, run.text, run.customer_name, self.business.today(), index)
                text = await self.supervisor.run_turn(context, tools, turn)
        await store.complete_run(run, assistant_message_id(run.id), text or EMPTY_REPLY)


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
