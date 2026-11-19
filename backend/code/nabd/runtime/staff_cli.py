"""Developer-only real-org staff loop smoke. No model calls; all test writes are cleaned up."""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from datetime import datetime, timezone

import httpx

from nabd.database import open_database
from nabd.migrations import migrate
from nabd.runtime.staff_store import TOPIC
from nabd.runtime.staff_sync import StaffSync
from nabd.runtime.store import Store
from nabd.salesforce.commands import CaseCommand, Evidence, SalesforceCommands, salesforce_time
from nabd.salesforce.pubsub import PubSubClient
from nabd.salesforce.reads import SalesforceReads
from nabd.salesforce.rest import CliOrgAuth, ClientCredentialsAuth, SalesforceClient
from nabd.settings import SalesforceSettings, Settings, StaffSyncSettings


def add_parser(commands):
    parser = commands.add_parser('staff-sync', help='staff synchronization verification')
    sub = parser.add_subparsers(dest='action', required=True)
    smoke = sub.add_parser('smoke', help='real Salesforce/PubSub/PostgreSQL staff loop (no model calls)')
    smoke.add_argument('--target-org', default='nabd-dev')
    smoke.add_argument('--samples', type=int, default=10, choices=range(5, 31), metavar='5..30')


class Smoke:
    def __init__(self, admin, integration, store, sync):
        self.admin, self.integration, self.store, self.sync = admin, integration, store, sync
        self.results, self.comments, self.latencies = [], [], []
        self.case_id = self.conversation = self.other = None
        self.task = None

    def check(self, name, passed, detail=''):
        self.results.append((name, bool(passed), detail))
        if not passed:
            raise RuntimeError(name)

    async def wait_event(self, kind, after=0, predicate=lambda e: True, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for event in await self.store.events_after(self.conversation, after):
                if event['type'] == kind and predicate(event):
                    return event
            await asyncio.sleep(.1)
        raise TimeoutError(f'waiting for {kind}')

    async def start(self):
        self.task = asyncio.create_task(self.sync.run_forever())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def comment(self, body, public=True):
        result = await self.admin.create('CaseComment', [{'ParentId':self.case_id, 'CommentBody':body,
                                                          'IsPublished':public}])
        self.comments.append(result[0]['id'])
        return result[0]['id']

    async def run(self, samples):
        tag = uuid.uuid4().hex[:12]
        info = await self.sync.subscriber.topic(TOPIC)
        self.check('integration can subscribe but cannot publish', info.can_subscribe and not info.can_publish)
        self.conversation = (await self.store.create_conversation('C008', 'Staff sync smoke'))['id']
        self.other = (await self.store.create_conversation('C006', 'Staff sync privacy smoke'))['id']
        result = await SalesforceCommands(self.integration).execute(CaseCommand(
            operation_id=f'staff-smoke-{tag}', operation='create_case', customer_id='C008',
            conversation_id=self.conversation, issue_id=f'staff-smoke-{tag}',
            category='Returns_Refunds', subject=f'NABD staff sync smoke {tag} (test)',
            summary='Staff synchronization verification; closed as duplicate afterwards.',
            language='en', sentiment='neutral', urgency='low', confidence=.99, needs_review=False,
            priority='Low', priority_reason='Low: smoke test', staff_action='refund',
            evidence=(Evidence(ref=f'staff-smoke-{tag}-msg', kind='customer_report', text='Staff sync test.',
                               language='en', script='latin', captured_at=salesforce_time(datetime.now(timezone.utc))),)))
        self.check('case created through the restricted gateway', result.status == 'confirmed')
        self.case_id = result.case_id
        accepted, _ = await self.store.submit_message(self.conversation, f'staff-smoke-{tag}', 'Staff sync test.')
        # This smoke does not run a customer turn; end its queued job explicitly.
        async with self.store.pool.connection() as conn:
            await conn.execute("UPDATE nabd.runs SET status = 'failed', error_code = 'smoke_no_model', "
                               "lease_owner = NULL, lease_expires_at = NULL WHERE id = %s", (accepted['run_id'],))
        await self.store.record_case(self.conversation, 'C008', accepted['run_id'], case_id=self.case_id,
                                    case_number=result.case_number, department='Returns & Refunds',
                                    status=result.case_status, operation='create_case')
        # Baseline before opening the subscriber; all measured changes below must arrive through Pub/Sub.
        await self.sync.process_once()
        await self.start()
        await asyncio.sleep(3)
        warmup = await self.comment(f'Warmup {tag}')
        event = await self.wait_event('staff.reply', predicate=lambda e: e['payload']['text'] == f'Warmup {tag}')
        after = event['sequence']
        self.check('public staff reply delivered through the feed', event['payload']['case_number'] == result.case_number)
        self.check('correct customer only', not (await self.store.snapshot(self.other))['staff_replies'])
        for n in range(samples):
            started = datetime.now(timezone.utc)
            status = 'Working' if n % 2 == 0 else 'Waiting on Staff'
            await self.admin.update('Case', [{'Id':self.case_id,'Status':status}])
            event = await self.wait_event('case.staff_update', after, lambda e: e['payload']['status'] == status)
            after = event['sequence']
            self.latencies.append((datetime.fromisoformat(event['created_at']) - started).total_seconds())
        p95 = sorted(self.latencies)[math.ceil(.95 * len(self.latencies))-1]
        self.check('healthy staff-change-to-durable-event p95 <= 5 s', p95 <= 5,
                   f'n={samples}, p95={p95:.3f}s, max={max(self.latencies):.3f}s')
        await self.admin.update('Case', [{'Id':self.case_id, 'Staff_Action_Completed__c':True}])
        event = await self.wait_event('case.staff_update', after,
                                     lambda e: e['payload']['staff_action_completed'])
        after = event['sequence']
        self.check('staff action completion delivered', 'staff_action_completed' in event['payload']['changes'])
        secret_note = f'PRIVATE staff smoke {tag}'
        await self.comment(secret_note, False)
        await self.sync.inbox.reconcile(self.conversation)
        await asyncio.sleep(2)
        self.check('private note absent from snapshot and replay', secret_note not in str(await self.store.snapshot(self.conversation))
                   and secret_note not in str(await self.store.events_after(self.conversation, 0)))
        await self.admin.update('CaseComment', [{'Id':warmup, 'CommentBody':f'Edited {tag}'}])
        event = await self.wait_event('staff.reply', after, lambda e: e['payload']['text'] == f'Edited {tag}')
        reply_id, after = event['payload']['reply_id'], event['sequence']
        self.check('public edit updates the same reply', event['payload']['revision'] == 2)
        await self.admin.update('CaseComment', [{'Id':warmup, 'IsPublished':False}])
        event = await self.wait_event('staff.reply.withdrawn', after, lambda e: e['payload']['reply_id'] == reply_id)
        after = event['sequence']
        self.check('unpublished text redacted from historical replay', f'Edited {tag}' not in str(
            await self.store.events_after(self.conversation, 0)))
        await self.stop()
        missed = f'Replayed {tag}'
        await self.comment(missed)
        await self.start()
        event = await self.wait_event('staff.reply', after, lambda e: e['payload']['text'] == missed)
        after = event['sequence']
        self.check('subscriber restart recovers missed reply', bool(await self.sync.inbox.cursor()))
        await self.sync.inbox.reconcile(self.conversation)
        await asyncio.sleep(2)
        self.check('replay and reconciliation do not duplicate replies', len([
            r for r in (await self.store.snapshot(self.conversation))['staff_replies'] if r['text'] == missed]) == 1)
        await self.admin.request('DELETE', f'{self.admin.base_path}/sobjects/CaseComment/{self.comments[-1]}')
        await self.wait_event('staff.reply.withdrawn', after)
        self.check('deleted public reply withdrawn', not (await self.store.snapshot(self.conversation))['staff_replies'])
        return self.results

    async def cleanup(self):
        await self.stop()
        for cid in self.comments:
            with contextlib.suppress(Exception):
                await self.admin.request('DELETE', f'{self.admin.base_path}/sobjects/CaseComment/{cid}')
        if self.case_id:
            await self.admin.update('Case', [{'Id':self.case_id,'Status':'Closed','Resolution_Outcome__c':'duplicate'}])
            await self.sync.inbox.reconcile(self.conversation)
            for _ in range(5):
                if not await self.sync.process_once():
                    break
        if self.case_id:
            print(f'Smoke case {self.case_id} closed as duplicate; test comments deleted.')


async def run(target_org, samples):
    settings, salesforce = Settings(), SalesforceSettings()
    await migrate(settings)
    async with open_database(settings) as db, httpx.AsyncClient(timeout=30) as http:
        auth = ClientCredentialsAuth(salesforce.domain_url, salesforce.client_id, salesforce.client_secret, http)
        integration = SalesforceClient(auth, http, salesforce.api_version)
        admin = SalesforceClient(CliOrgAuth(target_org), http, salesforce.api_version)
        store = Store(db.pool, lock_conninfo=settings.database_url.get_secret_value())
        sync = StaffSync(store, SalesforceReads(integration), PubSubClient(auth), None,
                         StaffSyncSettings(_env_file=None, reconcile_seconds=3600))
        smoke = Smoke(admin, integration, store, sync)
        try:
            await smoke.run(samples)
        except Exception as error:
            smoke.results.append(('staff loop completed', False, type(error).__name__ + ': ' + str(error)))
        finally:
            await smoke.cleanup()
        from nabd.salesforce.cli import _report
        return _report(smoke.results)
