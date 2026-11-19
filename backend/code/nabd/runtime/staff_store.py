"""Durable staff-feed inbox, projection and translation jobs.

The cursor and inbox commit together. Leases carry random fencing tokens, so
an expired processor cannot overwrite a newer projection or translation.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nabd.runtime.events import NewEvent
from nabd.runtime.staff_projection import ShownLink, ShownReply, normalized_version, plan_case_view, reply_payload
from nabd.salesforce.pubsub import EventBatch
from nabd.salesforce.soql import SALESFORCE_ID

TOPIC = "/event/NABD_Case_Change__e"


class StaffStore:
    def __init__(self, store):
        self.store = store
        self.pool = store.pool

    @contextlib.asynccontextmanager
    async def subscriber_lock(self):
        """One subscriber per database. The same session commits its inbox/cursor."""
        async with contextlib.AsyncExitStack() as stack:
            if self.store._lock_conninfo:
                conn = await AsyncConnection.connect(self.store._lock_conninfo, autocommit=True, row_factory=dict_row)
                stack.push_async_callback(conn.close)
            else:
                conn = await stack.enter_async_context(self.pool.connection())
            row = await (await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('nabd-staff-subscriber', 0)) AS locked")).fetchone()
            try:
                yield conn if row["locked"] else None
            finally:
                if row["locked"]:
                    with contextlib.suppress(Exception):
                        await conn.execute("SELECT pg_advisory_unlock(hashtextextended('nabd-staff-subscriber', 0))")

    async def cursor(self) -> bytes | None:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT replay_id FROM nabd.sync_cursors WHERE topic = %s",
                                           (TOPIC,))).fetchone()
        return bytes(row["replay_id"]) if row else None

    async def reset_cursor(self, leader=None) -> None:
        async with contextlib.AsyncExitStack() as stack:
            conn = leader or await stack.enter_async_context(self.pool.connection())
            await conn.execute("DELETE FROM nabd.sync_cursors WHERE topic = %s", (TOPIC,))

    @staticmethod
    async def _queue(conn, case_id: str, reason: str, source_at=None):
        await conn.execute(
            "INSERT INTO nabd.case_syncs (case_id, reason, source_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (case_id) DO UPDATE SET generation = nabd.case_syncs.generation + 1, "
            "reason = excluded.reason, source_at = excluded.source_at, requested_at = now(), not_before = now()",
            (case_id, reason, source_at))

    async def ingest(self, batch: EventBatch, leader=None) -> None:
        async with contextlib.AsyncExitStack() as stack:
            conn = leader or await stack.enter_async_context(self.pool.connection())
            await stack.enter_async_context(conn.transaction())
            for event in batch.events:
                case_id = event.fields.get("Case_Id__c")
                if not isinstance(case_id, str) or not SALESFORCE_ID.fullmatch(case_id):
                    raise ValueError("invalid staff event case ID")
                linked = await (await conn.execute("SELECT 1 FROM nabd.issue_links WHERE case_id = %s LIMIT 1",
                                                   (case_id,))).fetchone()
                if linked:
                    source = event.fields.get("CreatedDate")
                    at = datetime.fromtimestamp(source / 1000, timezone.utc) if isinstance(source, (float, int)) else None
                    await self._queue(conn, case_id, "event", at)
            if batch.latest_replay_id:
                await conn.execute(
                    "INSERT INTO nabd.sync_cursors (topic, replay_id) VALUES (%s, %s) "
                    "ON CONFLICT (topic) DO UPDATE SET replay_id = excluded.replay_id, updated_at = now()",
                    (TOPIC, batch.latest_replay_id))

    async def reconcile(self, conversation_id: str | None = None) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            rows = await (await conn.execute(
                "SELECT DISTINCT case_id FROM nabd.issue_links "
                "WHERE (%s::uuid IS NULL OR conversation_id = %s::uuid) ORDER BY case_id",
                (conversation_id, conversation_id))).fetchall()
            for row in rows:
                # Do not raise the generation of work already queued by the feed.
                await conn.execute("INSERT INTO nabd.case_syncs (case_id, reason) VALUES (%s, 'reconcile') "
                                   "ON CONFLICT (case_id) DO NOTHING", (row["case_id"],))

    async def claim(self, lease_seconds: float = 60) -> dict | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(
                "UPDATE nabd.case_syncs SET lease_token = %s, lease_until = now() + %s, attempts = attempts + 1 "
                "WHERE case_id = (SELECT case_id FROM nabd.case_syncs WHERE not_before <= now() "
                "AND (lease_until IS NULL OR lease_until < now()) ORDER BY requested_at LIMIT 1 "
                "FOR UPDATE SKIP LOCKED) RETURNING *", (uuid.uuid4(), timedelta(seconds=lease_seconds)))).fetchone()

    async def retry(self, job: dict, error: str, delay: float = 1) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE nabd.case_syncs SET lease_until = NULL, lease_token = NULL, "
                "not_before = now() + %s, last_error = %s WHERE case_id = %s AND lease_token = %s",
                (timedelta(seconds=delay), error, job["case_id"], job["lease_token"]))

    async def customer(self, case_id: str) -> str | None:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT customer_id FROM nabd.issue_links WHERE case_id = %s LIMIT 1",
                                           (case_id,))).fetchone()
        return row["customer_id"] if row else None

    async def in_flight(self, customer_id: str) -> bool:
        async with self.pool.connection() as conn:
            return bool(await (await conn.execute(
                "SELECT 1 FROM nabd.case_commands WHERE customer_id = %s "
                "AND status IN ('pending', 'dispatched', 'unknown') LIMIT 1", (customer_id,))).fetchone())

    @staticmethod
    async def _redact(conn, conversation_id, reply_id):
        # Offline SSE replay must not disclose a previous public revision after withdrawal/edit.
        await conn.execute(
            "UPDATE nabd.events SET type = 'staff.reply.withdrawn', code = 'staff.reply.withdrawn', "
            "payload = jsonb_build_object('reply_id', payload->>'reply_id', 'case_number', payload->>'case_number') "
            "WHERE conversation_id = %s AND type = 'staff.reply' AND payload->>'reply_id' = %s",
            (conversation_id, str(reply_id)))

    async def project(self, job: dict, state) -> bool:
        """Fenced, atomic projection plus events for every conversation linked to the case."""
        notified = []
        async with self.pool.connection() as conn, conn.transaction():
            current = await (await conn.execute("SELECT * FROM nabd.case_syncs WHERE case_id = %s FOR UPDATE",
                                               (job["case_id"],))).fetchone()
            if not current or current["lease_token"] != job["lease_token"]:
                return False
            if current["generation"] != job["generation"]:
                await conn.execute("UPDATE nabd.case_syncs SET lease_until = NULL, lease_token = NULL "
                                   "WHERE case_id = %s", (job["case_id"],))
                return False  # an event arrived while Salesforce was being read
            links = await (await conn.execute(
                "SELECT l.*, c.customer_id AS owner_customer FROM nabd.issue_links l "
                "JOIN nabd.conversations c ON c.id = l.conversation_id WHERE l.case_id = %s "
                "ORDER BY l.conversation_id FOR UPDATE OF c, l", (job["case_id"],))).fetchall()
            versions = await (await conn.execute(
                "SELECT result->>'case_version' AS version FROM nabd.case_commands "
                "WHERE status = 'confirmed' AND result->>'case_id' = %s", (job["case_id"],))).fetchall()
            own = {normalized_version(r["version"]) for r in versions}
            now = datetime.now(timezone.utc)
            for link in links:
                cid = str(link["conversation_id"])
                rows = await (await conn.execute("SELECT * FROM nabd.staff_replies WHERE conversation_id = %s "
                                                "AND case_id = %s FOR UPDATE", (cid, job["case_id"]))).fetchall()
                shown = [ShownReply(str(r["id"]), r["comment_id"], r["body"], r["revision"], r["posted_at"],
                                    r["withdrawn_at"] is not None, r["translation_status"], r["translation"],
                                    r["target_language"]) for r in rows]
                language = await (await conn.execute(
                    "SELECT payload FROM nabd.events WHERE conversation_id = %s AND type = 'conversation.language' "
                    "ORDER BY sequence DESC LIMIT 1", (cid,))).fetchone()
                anchor = await (await conn.execute("SELECT id FROM nabd.messages WHERE conversation_id = %s "
                                                  "ORDER BY created_at DESC, id DESC LIMIT 1", (cid,))).fetchone()
                anchor_id = str(anchor["id"]) if anchor else None
                plan = plan_case_view(ShownLink(cid, link["case_id"], link["owner_customer"], link["case_number"],
                    link["status"], link["staff_action"], link["staff_action_completed"], link["removed_at"] is not None,
                    link["synced_at"] is not None, link["created_at"]), shown, state, own,
                    language["payload"] if language else None, now)
                if plan.link:
                    # Column names come only from the pure projection, never external input.
                    assignments = ", ".join(f"{key} = %s" for key in plan.link)
                    await conn.execute(f"UPDATE nabd.issue_links SET {assignments}, updated_at = now() "
                                       "WHERE conversation_id = %s AND case_id = %s",
                                       (*plan.link.values(), cid, job["case_id"]))
                for reply in plan.new_replies:
                    await conn.execute(
                        "INSERT INTO nabd.staff_replies (id, conversation_id, comment_id, case_id, case_number, "
                        "body, posted_at, translation_status, target_language, after_message_id, source_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (reply["id"], cid, reply["comment_id"], job["case_id"], state.case_number, reply["body"],
                         reply["posted_at"], reply["translation_status"], Jsonb(reply["target_language"]),
                         anchor_id, job["source_at"]))
                for reply in plan.revised_replies:
                    await self._redact(conn, cid, reply["id"])
                    await conn.execute(
                        "UPDATE nabd.staff_replies SET body = %s, revision = %s, translation_status = %s, "
                        "target_language = %s, translation = NULL, translation_attempts = 0, "
                        "translation_lease_until = NULL, translation_token = NULL, withdrawn_at = NULL, "
                        "updated_at = now() WHERE id = %s",
                        (reply["body"], reply["revision"], reply["translation_status"], Jsonb(reply["target_language"]),
                         reply["id"]))
                for reply_id in plan.withdrawn:
                    await self._redact(conn, cid, reply_id)
                    await conn.execute(
                        "UPDATE nabd.staff_replies SET body = '', translation = NULL, target_language = NULL, "
                        "translation_status = 'none', translation_token = NULL, withdrawn_at = now(), "
                        "updated_at = now() WHERE id = %s", (reply_id,))
                for event in plan.events:
                    if event.type == "staff.reply":
                        prior = next((r for r in rows if str(r["id"]) == event.payload["reply_id"]), None)
                        event.payload["after_message_id"] = str(prior["after_message_id"]) if prior and prior["after_message_id"] else anchor_id
                    else:
                        event.payload["after_message_id"] = anchor_id
                    await self.store._append(conn, cid, event)
                if plan.events:
                    notified.append(cid)
            await conn.execute("DELETE FROM nabd.case_syncs WHERE case_id = %s", (job["case_id"],))
        for cid in notified:
            self.store._notify_local(cid)
        return True

    async def claim_translation(self, lease_seconds=60):
        async with self.pool.connection() as conn:
            return await (await conn.execute(
                "UPDATE nabd.staff_replies SET translation_token = %s, translation_lease_until = now() + %s, "
                "translation_attempts = translation_attempts + 1 WHERE id = (SELECT id FROM nabd.staff_replies "
                "WHERE translation_status = 'pending' AND withdrawn_at IS NULL "
                "AND (translation_lease_until IS NULL OR translation_lease_until < now()) "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
                (uuid.uuid4(), timedelta(seconds=lease_seconds)))).fetchone()

    async def finish_translation(self, job, translation: dict | None):
        cid = str(job["conversation_id"])
        async with self.pool.connection() as conn, conn.transaction():
            # Same order as projection and event appends.
            await conn.execute("SELECT 1 FROM nabd.conversations WHERE id = %s FOR UPDATE", (cid,))
            row = await (await conn.execute(
                "UPDATE nabd.staff_replies SET translation_status = %s, translation = %s, "
                "translation_lease_until = NULL, translation_token = NULL, updated_at = now() "
                "WHERE id = %s AND revision = %s AND translation_token = %s AND withdrawn_at IS NULL "
                "RETURNING *", ("done" if translation else "failed", Jsonb(translation), job["id"],
                                 job["revision"], job["translation_token"]))).fetchone()
            if row is None:
                return False
            payload = reply_payload(str(row["id"]), row["case_number"], row["body"], row["posted_at"],
                                    row["revision"], row["translation_status"], translation)
            payload["after_message_id"] = str(row["after_message_id"]) if row["after_message_id"] else None
            await self.store._append(conn, cid, NewEvent(type="staff.reply", code="staff.reply", payload=payload))
        self.store._notify_local(cid)
        return True

    async def history(self, conversation_id, limit=20):
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id, case_number, body, posted_at FROM nabd.staff_replies WHERE conversation_id = %s "
                "AND withdrawn_at IS NULL ORDER BY posted_at DESC LIMIT %s", (conversation_id, limit))).fetchall()
        return [{"id": str(r["id"]), "case_number": r["case_number"], "text": r["body"], "at": r["posted_at"]}
                for r in reversed(rows)]
