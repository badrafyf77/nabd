"""Free staff synchronization tests, including real PostgreSQL recovery/privacy."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeChatModel, say
from nabd.agents.staff_translation import StaffTranslator
from nabd.database import open_database
from nabd.migrations import migrate
from nabd.runtime.events import NewEvent
from nabd.runtime.staff_store import StaffStore, TOPIC
from nabd.runtime.staff_sync import StaffSync
from nabd.runtime.store import Store, NewCommand
from nabd.salesforce.pubsub import EventBatch, DecodedEvent
from nabd.salesforce.reads import CaseState, PublicReply, RecordRef
from nabd.settings import StaffSyncSettings
from test_postgres import empty_database  # noqa: F401

CASE = '500000000000001AAA'
COMMENT = '00a000000000001AAA'
NOW = datetime.now(timezone.utc)
FR = dict(input_language='fr', reply_language='fr', reply_script='latin')


def state(**changes):
    return CaseState(case_id=CASE, case_number='00002001', customer_id='C003', status='New', is_closed=False,
                     resolution=None, staff_action='Refund', staff_action_completed=False, replies=(),
                     record=RecordRef(sobject='Case', id=CASE, version=NOW)).model_copy(update=changes)


def comment(body='Refund GBP 34.99 for SE-1061 in 3 days.', **changes):
    return PublicReply(comment_id=COMMENT, body=body, posted_at=NOW + timedelta(days=1), edited_at=None,
                       **{}).model_copy(update=changes)


@pytest.fixture
def database(empty_database):
    asyncio.run(migrate(empty_database))
    return empty_database


async def setup(db):
    store = Store(db.pool, lock_conninfo=db.pool.conninfo)
    conv = (await store.create_conversation('C003', 'Amina'))['id']
    outsider = (await store.create_conversation('C004', 'Other'))['id']
    accepted, _ = await store.submit_message(conv, 'staff-test-0001', 'Where is my refund?')
    await store.record_case(conv, 'C003', accepted['run_id'], case_id=CASE, case_number='00002001',
                            department='Returns & Refunds', status='New', operation='create_case')
    return store, StaffStore(store), conv, outsider, accepted


async def project(inbox, current):
    await inbox.reconcile()
    job = await inbox.claim()
    assert job
    return await inbox.project(job, current)


def test_public_projection_replay_edits_withdrawal_reparent_and_restore(database):
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, other, accepted = await setup(db)
            await store.emit(conv, NewEvent(type='conversation.language', payload=FR))
            assert await project(inbox, state())  # baseline
            assert not [e for e in await store.events_after(conv, 0) if e['type'].startswith('staff.')]
            current = state(status='Working', staff_action_completed=True, replies=(comment(),))
            assert await project(inbox, current)
            snapshot = await store.snapshot(conv)
            assert snapshot['cases'][0]['status'] == 'Working'
            reply = snapshot['staff_replies'][0]
            assert reply['text'] == comment().body and reply['translation']['status'] == 'pending'
            assert reply['after_message_id'] == accepted['message_id']
            events = await store.events_after(conv, 0)
            assert [e['type'] for e in events][-2:] == ['case.staff_update', 'staff.reply']
            assert all(e['run_id'] is None for e in events[-2:])
            assert (await store.snapshot(other))['staff_replies'] == []
            last = snapshot['last_sequence']
            await project(inbox, current)
            assert (await store.snapshot(conv))['last_sequence'] == last
            tr = await inbox.claim_translation()
            translated = dict(text='Remboursement GBP 34.99 pour SE-1061 dans 3 jours.', source_language='en', **FR)
            assert await inbox.finish_translation(tr, translated)
            assert not await inbox.finish_translation(tr, translated)
            # An edit invalidates a claimed translation and redacts the prior body from SSE replay.
            await project(inbox, state(replies=(comment('Edited public reply 42'),)))
            edited = (await store.snapshot(conv))['staff_replies'][0]
            assert edited['revision'] == 2 and edited['translation']['status'] == 'pending'
            assert not await inbox.finish_translation(tr, translated)
            assert comment().body not in str(await store.events_after(conv, 0))
            tr2 = await inbox.claim_translation()
            await project(inbox, state(replies=()))  # private or deleted; neither is readable
            assert (await store.snapshot(conv))['staff_replies'] == []
            assert await inbox.history(conv) == []
            assert 'Edited public reply 42' not in str(await store.events_after(conv, 0))
            assert not await inbox.finish_translation(tr2, translated)
            await project(inbox, current)  # public again
            assert (await store.snapshot(conv))['staff_replies'][0]['revision'] == 3
            await project(inbox, state(customer_id='C004', replies=(comment('Other customer text'),)))
            assert (await store.snapshot(conv))['cases'] == []
            assert (await store.snapshot(conv))['staff_replies'] == []
            assert (await store.snapshot(other))['staff_replies'] == []
            assert 'Other customer text' not in str(await store.events_after(conv, 0))
            await project(inbox, current)  # ownership restored
            assert (await store.snapshot(conv))['cases'][0]['status'] == 'Working'
            await project(inbox, None)  # case deleted
            assert (await store.snapshot(conv))['cases'] == []
            # Withdrawn text is not retained in the local reply rows either.
            async with db.pool.connection() as conn:
                row = await (await conn.execute('SELECT body, translation FROM nabd.staff_replies')).fetchone()
                assert row == {'body': '', 'translation': None}
    asyncio.run(run())


def test_cursor_inbox_atomicity_generation_and_expired_leases(database):
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, _, _ = await setup(db)
            await project(inbox, state())
            event = DecodedEvent(b'replay-1', 'one', {'Case_Id__c': CASE})
            # The second invalid event rolls back the first marker AND the new cursor.
            bad = DecodedEvent(b'bad', 'bad', {'Case_Id__c': 'invalid'})
            with pytest.raises(ValueError):
                await inbox.ingest(EventBatch((event, bad), b'bad'))
            assert await inbox.cursor() is None and await inbox.claim() is None
            await inbox.ingest(EventBatch((event,), b'replay-1'))
            job = await inbox.claim(0.01)
            await inbox.ingest(EventBatch((event,), b'replay-2'))
            assert await inbox.cursor() == b'replay-2'
            assert not await inbox.project(job, state(status='Working'))  # newer generation wins
            newer = await inbox.claim(0.01)
            await asyncio.sleep(0.02)
            reclaimed = await inbox.claim()
            assert reclaimed['lease_token'] != newer['lease_token']
            assert not await inbox.project(newer, state(status='Closed'))
            assert await inbox.project(reclaimed, state(status='Working'))
            assert (await store.snapshot(conv))['cases'][0]['status'] == 'Working'
            await inbox.ingest(EventBatch((), b'keepalive'))
            assert await inbox.cursor() == b'keepalive'
            assert await inbox.claim() is None
            async with inbox.subscriber_lock() as leader:
                assert leader
                async with inbox.subscriber_lock() as second:
                    assert second is None
            async with inbox.subscriber_lock() as third:
                assert third
    asyncio.run(run())


def test_echoes_pending_commands_reconciliation_and_translation_failures(database):
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, _, accepted = await setup(db)
            class Reads:
                current = state()
                async def case_states(self, ids):
                    return {CASE: self.current}
            class Translator:
                async def translate(self, body, language):
                    raise ValueError('unavailable')
            reads = Reads()
            sync = StaffSync(store, reads, None, Translator(), StaffSyncSettings(_env_file=None))
            await sync.process_once()
            await store.emit(conv, NewEvent(type='conversation.language', payload=FR))
            cmd = NewCommand('staff-op-0001', '1', 'issue-1', 'update_case', {'case_id': CASE})
            await store.add_command(accepted['run_id'], conv, 'C003', cmd)
            reads.current = state(status='Working')
            await inbox.reconcile()
            await sync.process_once()
            assert (await store.snapshot(conv))['cases'][0]['status'] == 'New'
            await store.mark_command(cmd.operation_id, 'confirmed', result={
                'case_id': CASE, 'case_version': NOW.isoformat().replace('+00:00', 'Z')})
            async with db.pool.connection() as conn:
                await conn.execute('UPDATE nabd.case_syncs SET not_before = now()')
            await sync.process_once()
            assert (await store.snapshot(conv))['cases'][0]['status'] == 'Working'
            assert not [e for e in await store.events_after(conv, 0) if e['type'] == 'case.staff_update']
            reads.current = state(status='Closed', record=RecordRef(sobject='Case', id=CASE,
                                  version=NOW+timedelta(seconds=5)), replies=(comment(),))
            await inbox.reconcile(conv)  # missed events, browser-open recovery
            await sync.process_once()
            assert (await store.snapshot(conv))['cases'][0]['status'] == 'Closed'
            assert await sync.translate_once()
            snap = await store.snapshot(conv)
            assert snap['staff_replies'][0]['translation']['status'] == 'failed'
            assert snap['staff_replies'][0]['text'] == comment().body
            assert (await store.staff_history(conv))[0]['text'] == comment().body
            assert not await sync.translate_once()
    asyncio.run(run())


def test_old_comments_do_not_cross_a_new_conversation_link(database):
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, _, _ = await setup(db)
            await project(inbox, state(replies=(comment(posted_at=NOW-timedelta(days=1)),)))
            assert (await store.snapshot(conv))['staff_replies'] == []
    asyncio.run(run())


@pytest.mark.parametrize('target,text', [(FR, 'Votre remboursement GBP 34.99 pour SE-1061 prend 3 jours.'),
    ({'reply_language': 'ar', 'reply_script': 'arabic'}, 'استرداد GBP 34.99 للطلب SE-1061 خلال 3 أيام.')])
def test_translation_is_structured_tool_free_and_keeps_numbers(target, text):
    async def run():
        import json
        model = FakeChatModel(replies=[say(json.dumps({'text': text, 'source_language': 'en'}))])
        result = await StaffTranslator(model).translate(comment().body, target)
        assert result['text'] == text
        assert model.requests[0]['tool_choice'] == 'none'
        assert model.requests[0]['response_format'].__name__ == 'Translation'
    asyncio.run(run())


def test_translation_rejects_lost_numbers_and_repairs_latin_darija():
    async def run():
        model = FakeChatModel(replies=[say('{"text":"Refund in 3 days","source_language":"en"}')])
        with pytest.raises(ValueError, match='numeric'):
            await StaffTranslator(model).translate(comment().body, FR)
        model = FakeChatModel(replies=[say('{"text":"Rje3na 34.99 GBP, باقي 3 days SE-1061", "source_language":"en"}'),
                                      say('Rje3na 34.99 GBP, باقي 3 days SE-1061')])
        result = await StaffTranslator(model).translate(comment().body, {'reply_language':'ary', 'reply_script':'latin'})
        assert 'باقي' not in result['text'] and '34.99' in result['text'] and 'SE-1061' in result['text']
        assert len(model.requests) == 2
    asyncio.run(run())


def test_subscriber_refresh_expired_replay_and_restart_reconcile(database):
    from nabd.salesforce.pubsub import ReplayRejected
    from nabd.salesforce.rest import SalesforceAuthError
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, _, _ = await setup(db)
            await project(inbox, state())
            await inbox.ingest(EventBatch((), b'expired'))
            connected = asyncio.Event()
            class Subscriber:
                calls = []
                async def subscribe(self, topic, cursor, refresh=False):
                    self.calls.append((cursor, refresh))
                    if len(self.calls) == 1:
                        raise SalesforceAuthError('expired token')
                    if len(self.calls) == 2:
                        raise ReplayRejected('expired cursor')
                    yield EventBatch((DecodedEvent(b'new', 'new', {'Case_Id__c':CASE}),), b'new')
                    connected.set()
                    await asyncio.Event().wait()
            class Reads:
                async def case_states(self, ids):
                    return {CASE: state(status='Working')}
            subscriber = Subscriber()
            sync = StaffSync(store, Reads(), subscriber, None,
                             StaffSyncSettings(_env_file=None, reconnect_max_seconds=1))
            task = asyncio.create_task(sync.run_forever())
            try:
                await asyncio.wait_for(connected.wait(), 8)
                end = asyncio.get_running_loop().time() + 3
                while (await store.snapshot(conv))['cases'][0]['status'] != 'Working':
                    assert asyncio.get_running_loop().time() < end
                    await asyncio.sleep(.05)
                assert subscriber.calls[0] == (b'expired', False)
                assert subscriber.calls[1] == (b'expired', True)
                assert subscriber.calls[2][0] is None
                assert await inbox.cursor() == b'new'
            finally:
                task.cancel()
                import contextlib
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    asyncio.run(run())


def test_translation_crash_lease_is_reclaimed_and_stale_worker_is_fenced(database):
    async def run():
        async with open_database(database) as db:
            store, inbox, conv, _, _ = await setup(db)
            await store.emit(conv, NewEvent(type='conversation.language', payload=FR))
            await project(inbox, state(replies=(comment(),)))
            old = await inbox.claim_translation(.01)
            assert await inbox.claim_translation() is None
            await asyncio.sleep(.02)
            current = await inbox.claim_translation()
            assert current['translation_token'] != old['translation_token']
            assert not await inbox.finish_translation(old, {'text':'stale'})
            assert await inbox.finish_translation(current, None)
            assert (await store.snapshot(conv))['staff_replies'][0]['translation']['status'] == 'failed'
    asyncio.run(run())
