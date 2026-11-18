"""PostgreSQL state of the chat: demo sessions, conversations, messages, runs, events.

Runs are a durable queue. A worker claims the oldest pending run of a
conversation while holding that conversation's row lock, so one conversation
never has two active runs, and renews a lease while it works. A run whose
lease expired (its worker died) is claimed again, up to ``max_attempts``.

Events get a per-conversation sequence number in the same transaction that
increments ``conversations.last_sequence``, and every append is announced
with ``NOTIFY nabd_events`` so SSE streams in any process wake up.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from nabd.runtime.events import NewEvent

NOTIFY_CHANNEL = "nabd_events"
SNAPSHOT_RUNS = 50


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


def _event_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "sequence": row["sequence"], "conversation_id": str(row["conversation_id"]),
        "run_id": _id(row["run_id"]), "type": row["type"], "activity_id": row["activity_id"],
        "parent_activity_id": row["parent_activity_id"], "state": row["state"], "code": row["code"],
        "payload": row["payload"], "created_at": row["created_at"].isoformat(),
    }


@dataclass(frozen=True)
class DemoSession:
    customer_id: str
    first_name: str
    display_name: str
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedRun:
    id: str
    conversation_id: str
    message_id: str
    customer_id: str
    customer_name: str
    text: str
    attempt: int
    lease_owner: str


class Store:
    def __init__(self, pool: AsyncConnectionPool, *, local_notify=None):
        self.pool = pool
        self._local_notify = local_notify  # in-process shortcut; NOTIFY reaches other processes

    # --- Demo sessions ----------------------------------------------------------------

    async def create_session(self, customer_id: str, first_name: str, display_name: str,
                             hours: float) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO nabd.demo_sessions (token_hash, customer_id, first_name, display_name, expires_at) "
                "VALUES (%s, %s, %s, %s, now() + %s) RETURNING expires_at",
                (_hash(token), customer_id, first_name, display_name, timedelta(hours=hours)),
            )).fetchone()
        return token, row["expires_at"]

    async def get_session(self, token: str | None) -> DemoSession | None:
        if not token or len(token) > 200:
            return None
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT customer_id, first_name, display_name, expires_at FROM nabd.demo_sessions "
                "WHERE token_hash = %s AND ended_at IS NULL AND expires_at > now()", (_hash(token),),
            )).fetchone()
        return DemoSession(**row) if row else None

    async def end_session(self, token: str | None) -> None:
        if not token:
            return
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE nabd.demo_sessions SET ended_at = now() WHERE token_hash = %s "
                               "AND ended_at IS NULL", (_hash(token),))

    # --- Conversations ----------------------------------------------------------------

    async def create_conversation(self, customer_id: str, customer_name: str) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO nabd.conversations (id, customer_id, customer_name) VALUES (%s, %s, %s) "
                "RETURNING id, title, created_at, updated_at, last_sequence",
                (uuid.uuid4(), customer_id, customer_name),
            )).fetchone()
        return self._conversation(row)

    @staticmethod
    def _conversation(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["id"]), "title": row["title"], "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(), "last_sequence": row["last_sequence"]}

    async def list_conversations(self, customer_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id, title, created_at, updated_at, last_sequence FROM nabd.conversations "
                "WHERE customer_id = %s ORDER BY updated_at DESC LIMIT 50", (customer_id,),
            )).fetchall()
        return [self._conversation(row) for row in rows]

    async def owns(self, customer_id: str, conversation_id: str) -> dict[str, Any] | None:
        """The conversation if it belongs to this customer; another customer's looks missing."""
        try:
            key = uuid.UUID(conversation_id)
        except ValueError:
            return None
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT id, title, created_at, updated_at, last_sequence FROM nabd.conversations "
                "WHERE id = %s AND customer_id = %s", (key, customer_id),
            )).fetchone()
        return self._conversation(row) if row else None

    # --- Events -----------------------------------------------------------------------

    async def _append(self, conn: AsyncConnection, conversation_id: str, event: NewEvent) -> int:
        row = await (await conn.execute(
            "WITH seq AS (UPDATE nabd.conversations SET last_sequence = last_sequence + 1, updated_at = now() "
            "WHERE id = %s RETURNING last_sequence) "
            "INSERT INTO nabd.events (conversation_id, sequence, id, run_id, type, activity_id, "
            "parent_activity_id, state, code, payload) "
            "SELECT %s, last_sequence, %s, %s, %s, %s, %s, %s, %s, %s FROM seq RETURNING sequence",
            (conversation_id, conversation_id, uuid.uuid4(), event.run_id, event.type, event.activity_id,
             event.parent_activity_id, event.state, event.code, Jsonb(event.payload)),
        )).fetchone()
        await conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, conversation_id))
        return row["sequence"]

    def _notify_local(self, conversation_id: str) -> None:
        if self._local_notify is not None:
            self._local_notify(conversation_id)

    async def emit(self, conversation_id: str, event: NewEvent) -> int:
        async with self.pool.connection() as conn, conn.transaction():
            sequence = await self._append(conn, conversation_id, event)
        self._notify_local(conversation_id)
        return sequence

    async def events_after(self, conversation_id: str, after: int, limit: int = 500) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM nabd.events WHERE conversation_id = %s AND sequence > %s "
                "ORDER BY sequence LIMIT %s", (conversation_id, after, limit),
            )).fetchall()
        return [_event_dict(row) for row in rows]

    # --- Messages and runs ------------------------------------------------------------------

    async def submit_message(self, conversation_id: str, client_message_id: str,
                             text: str) -> tuple[dict[str, Any], bool]:
        """Store a customer message and queue its run. A repeated client ID returns the first one."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM nabd.conversations WHERE id = %s FOR UPDATE", (conversation_id,))
            existing = await (await conn.execute(
                "SELECT m.id AS message_id, r.id AS run_id, r.status, r.turn FROM nabd.messages m "
                "JOIN nabd.runs r ON r.message_id = m.id "
                "WHERE m.conversation_id = %s AND m.client_message_id = %s",
                (conversation_id, client_message_id),
            )).fetchone()
            if existing:
                return self._accepted(existing), False
            message_id, run_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO nabd.messages (id, conversation_id, role, client_message_id, run_id, text) "
                "VALUES (%s, %s, 'customer', %s, %s, %s)",
                (message_id, conversation_id, client_message_id, run_id, text),
            )
            counts = await (await conn.execute(
                "SELECT coalesce(max(turn), 0) + 1 AS turn, "
                "count(*) FILTER (WHERE status IN ('queued', 'running')) AS ahead "
                "FROM nabd.runs WHERE conversation_id = %s", (conversation_id,),
            )).fetchone()
            await conn.execute(
                "INSERT INTO nabd.runs (id, conversation_id, message_id, turn, status) "
                "VALUES (%s, %s, %s, %s, 'queued')", (run_id, conversation_id, message_id, counts["turn"]),
            )
            await conn.execute(
                "UPDATE nabd.conversations SET title = coalesce(title, left(%s, 80)) WHERE id = %s",
                (text, conversation_id),
            )
            await self._append(conn, conversation_id, NewEvent(
                type="message.created", run_id=str(run_id), payload={
                    "message_id": str(message_id), "role": "customer", "text": text,
                    "client_message_id": client_message_id}))
            await self._append(conn, conversation_id, NewEvent(
                type="run.queued", run_id=str(run_id), state="queued",
                payload={"message_id": str(message_id), "ahead": counts["ahead"]}))
        self._notify_local(conversation_id)
        return {"message_id": str(message_id), "run_id": str(run_id), "status": "queued",
                "turn": counts["turn"]}, True

    @staticmethod
    def _accepted(row: dict[str, Any]) -> dict[str, Any]:
        return {"message_id": str(row["message_id"]), "run_id": str(row["run_id"]), "status": row["status"],
                "turn": row["turn"]}

    async def claim_run(self, worker_id: str, lease_seconds: float, max_attempts: int) -> ClaimedRun | None:
        """Claim the next runnable run, or None. Exhausted crashed runs are failed on the way."""
        lease = timedelta(seconds=lease_seconds)
        while True:
            exhausted = None
            async with self.pool.connection() as conn, conn.transaction():
                conversation = await (await conn.execute(
                    "SELECT c.id FROM nabd.conversations c "
                    "WHERE EXISTS (SELECT 1 FROM nabd.runs r WHERE r.conversation_id = c.id AND "
                    "  (r.status = 'queued' OR (r.status = 'running' AND r.lease_expires_at < now()))) "
                    "AND NOT EXISTS (SELECT 1 FROM nabd.runs r WHERE r.conversation_id = c.id AND "
                    "  r.status = 'running' AND r.lease_expires_at >= now()) "
                    "ORDER BY c.updated_at LIMIT 1 FOR UPDATE OF c SKIP LOCKED",
                )).fetchone()
                if conversation is None:
                    return None
                run = await (await conn.execute(
                    "SELECT r.*, m.text, c.customer_id, c.customer_name, "
                    "(r.status = 'running' AND r.lease_expires_at >= now()) AS live FROM nabd.runs r "
                    "JOIN nabd.messages m ON m.id = r.message_id JOIN nabd.conversations c ON c.id = r.conversation_id "
                    "WHERE r.conversation_id = %s AND r.status IN ('queued', 'running') ORDER BY r.turn LIMIT 1",
                    (conversation["id"],),
                )).fetchone()
                if run["live"]:
                    return None  # its worker is still alive
                if run["attempt"] >= max_attempts:
                    await conn.execute(
                        "UPDATE nabd.runs SET status = 'failed', error_code = 'attempts_exhausted', "
                        "finished_at = now(), lease_owner = NULL, lease_expires_at = NULL WHERE id = %s",
                        (run["id"],))
                    await self._append(conn, str(run["conversation_id"]), NewEvent(
                        type="run.failed", run_id=str(run["id"]), state="failed", code="run.failed.attempts",
                        payload={"error": "attempts_exhausted"}))
                    exhausted = str(run["conversation_id"])
                else:
                    claimed = await (await conn.execute(
                        "UPDATE nabd.runs SET status = 'running', attempt = attempt + 1, lease_owner = %s, "
                        "lease_expires_at = now() + %s, started_at = coalesce(started_at, now()) "
                        "WHERE id = %s RETURNING attempt", (worker_id, lease, run["id"]),
                    )).fetchone()
                    return ClaimedRun(
                        id=str(run["id"]), conversation_id=str(run["conversation_id"]),
                        message_id=str(run["message_id"]), customer_id=run["customer_id"],
                        customer_name=run["customer_name"], text=run["text"], attempt=claimed["attempt"],
                        lease_owner=worker_id)
            if exhausted:
                self._notify_local(exhausted)

    async def renew_lease(self, run: ClaimedRun, lease_seconds: float) -> bool:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE nabd.runs SET lease_expires_at = now() + %s WHERE id = %s AND status = 'running' "
                "AND lease_owner = %s AND attempt = %s AND lease_expires_at >= now()",
                (timedelta(seconds=lease_seconds), run.id, run.lease_owner, run.attempt))
        return cursor.rowcount == 1

    async def _finish(self, run: ClaimedRun, status: str, error: str | None,
                      events: list[NewEvent], reply: tuple[str, str] | None = None) -> bool:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                "UPDATE nabd.runs SET status = %s, error_code = %s, finished_at = now(), lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = %s AND status = 'running' AND lease_owner = %s "
                "AND attempt = %s", (status, error, run.id, run.lease_owner, run.attempt))
            if cursor.rowcount != 1:
                return False  # the lease was lost; another attempt owns the run now
            if reply is not None:
                await conn.execute(
                    "INSERT INTO nabd.messages (id, conversation_id, role, run_id, text) "
                    "VALUES (%s, %s, 'assistant', %s, %s) ON CONFLICT (id) DO NOTHING",
                    (reply[0], run.conversation_id, run.id, reply[1]))
            for event in events:
                await self._append(conn, run.conversation_id, event.model_copy(update={"run_id": run.id}))
        self._notify_local(run.conversation_id)
        return True

    async def complete_run(self, run: ClaimedRun, message_id: str, text: str) -> bool:
        return await self._finish(run, "completed", None, [
            NewEvent(type="message.completed", payload={"message_id": message_id, "role": "assistant",
                                                        "text": text, "attempt": run.attempt}),
            NewEvent(type="run.completed", state="completed"),
        ], reply=(message_id, text))

    async def fail_run(self, run: ClaimedRun, error: str) -> bool:
        return await self._finish(run, "failed", error, [
            NewEvent(type="run.failed", state="failed", code=f"run.failed.{error}", payload={"error": error})])

    async def release_run(self, run: ClaimedRun, error: str) -> bool:
        """Put a failed attempt back in the queue for another attempt."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE nabd.runs SET status = 'queued', error_code = %s, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = %s AND status = 'running' AND lease_owner = %s "
                "AND attempt = %s", (error, run.id, run.lease_owner, run.attempt))
        return cursor.rowcount == 1

    # --- Snapshot ----------------------------------------------------------------------------

    async def snapshot(self, conversation_id: str) -> dict[str, Any]:
        """Current state for a (re)loading browser: messages, runs, their activity and partial replies."""
        async with self.pool.connection() as conn, conn.transaction():
            conversation = await (await conn.execute(
                "SELECT last_sequence FROM nabd.conversations WHERE id = %s", (conversation_id,))).fetchone()
            messages = await (await conn.execute(
                "SELECT m.id, m.role, m.run_id, m.text, m.created_at FROM nabd.messages m "
                "JOIN nabd.runs r ON r.id = m.run_id WHERE m.conversation_id = %s "
                "ORDER BY r.turn, m.role = 'assistant'", (conversation_id,))).fetchall()
            runs = await (await conn.execute(
                "SELECT id, message_id, turn, status, attempt, error_code, created_at, started_at, finished_at "
                "FROM nabd.runs WHERE conversation_id = %s ORDER BY turn DESC LIMIT %s",
                (conversation_id, SNAPSHOT_RUNS))).fetchall()
            run_ids = [row["id"] for row in runs]
            events = await (await conn.execute(
                "SELECT * FROM nabd.events WHERE conversation_id = %s AND run_id = ANY(%s) "
                "AND type NOT IN ('message.delta', 'message.created', 'message.completed') "
                "AND sequence <= %s ORDER BY sequence",
                (conversation_id, run_ids, conversation["last_sequence"]))).fetchall()
            active = [row["id"] for row in runs if row["status"] == "running"]
            deltas = await (await conn.execute(
                "SELECT run_id, payload FROM nabd.events WHERE conversation_id = %s AND run_id = ANY(%s) "
                "AND type = 'message.delta' AND sequence <= %s ORDER BY sequence",
                (conversation_id, active, conversation["last_sequence"]))).fetchall()
        attempts = {str(row["id"]): row["attempt"] for row in runs}
        partial: dict[str, dict[str, Any]] = {}
        for row in deltas:
            payload = row["payload"]
            if payload.get("attempt") != attempts.get(str(row["run_id"])):
                continue  # text of an earlier, abandoned attempt
            item = partial.setdefault(payload["message_id"], {"run_id": str(row["run_id"]), "text": ""})
            item["text"] += payload["text"]
        return {
            "last_sequence": conversation["last_sequence"],
            "messages": [{"id": str(m["id"]), "role": m["role"], "run_id": _id(m["run_id"]), "text": m["text"],
                          "created_at": m["created_at"].isoformat()} for m in messages],
            "runs": [{"id": str(r["id"]), "message_id": str(r["message_id"]), "turn": r["turn"],
                      "status": r["status"], "attempt": r["attempt"], "error": r["error_code"],
                      "created_at": r["created_at"].isoformat()} for r in reversed(runs)],
            "events": [_event_dict(row) for row in events],
            "partial": partial,
        }
