"""Salesforce subscriber, current-state processor, reconciliation and translation loops."""
from __future__ import annotations

import asyncio
import logging
import random

from nabd.runtime.staff_store import StaffStore, TOPIC
from nabd.salesforce.pubsub import ReplayRejected
from nabd.salesforce.rest import SalesforceAuthError

log = logging.getLogger(__name__)


class StaffSync:
    def __init__(self, store, reads, subscriber, translator, settings):
        self.store = store
        self.inbox = StaffStore(store)
        self.reads = reads
        self.subscriber = subscriber
        self.translator = translator
        self.settings = settings

    async def run_forever(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self._subscribe_forever())
            group.create_task(self._loop(self.process_once, self.settings.poll_seconds))
            group.create_task(self._loop(self.translate_once, self.settings.poll_seconds))
            group.create_task(self._loop(self.inbox.reconcile, self.settings.reconcile_seconds))

    async def _loop(self, action, interval):
        while True:
            try:
                worked = await action()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("Staff sync pass failed (%s)", type(error).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(interval)

    async def _subscribe_forever(self):
        delay, refresh = 1.0, False
        while True:
            try:
                async with self.inbox.subscriber_lock() as leader:
                    if leader is not None:
                        await self.inbox.reconcile()
                        cursor = await self.inbox.cursor()
                        try:
                            first = True
                            async for batch in self.subscriber.subscribe(TOPIC, cursor, refresh=refresh):
                                await self.inbox.ingest(batch, leader)
                                if first:
                                    await self.inbox.reconcile()
                                    first = False
                                delay, refresh = 1.0, False
                        except ReplayRejected:
                            # Clear while still leader, so a retiring subscriber cannot erase its successor's cursor.
                            await self.inbox.reset_cursor(leader)
                            await self.inbox.reconcile()
                            log.warning("Staff replay cursor expired; reconciling current case state")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                refresh = isinstance(error, SalesforceAuthError)
                log.warning("Staff subscriber disconnected (%s)", type(error).__name__)
            await asyncio.sleep(delay + random.random() * 0.2)
            delay = min(delay * 2, self.settings.reconnect_max_seconds)

    async def process_once(self):
        job = await self.inbox.claim()
        if job is None:
            return False
        try:
            customer = await self.inbox.customer(job["case_id"])
            if customer:
                # Serialize with NABD case writes, including commands that are still ambiguous.
                async with self.store.case_lock(customer):
                    if await self.inbox.in_flight(customer):
                        await self.inbox.retry(job, "case_command_in_flight")
                        return True
                    states = await self.reads.case_states([job["case_id"]])
                    await self.inbox.project(job, states.get(job["case_id"]))
            else:
                await self.inbox.project(job, None)
        except asyncio.CancelledError:
            raise  # the lease makes this work recoverable
        except Exception as error:
            await self.inbox.retry(job, type(error).__name__, min(2 ** min(job["attempts"], 6), 60))
            log.warning("Staff case sync failed (%s)", type(error).__name__)
        return True

    async def translate_once(self):
        job = await self.inbox.claim_translation()
        if job is None:
            return False
        translation = None
        try:
            if job["translation_attempts"] <= 3:
                async with asyncio.timeout(self.settings.translation_timeout_seconds):
                    translation = await self.translator.translate(job["body"], job["target_language"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Staff reply translation unavailable (%s)", type(error).__name__)
        await self.inbox.finish_translation(job, translation)
        return True
