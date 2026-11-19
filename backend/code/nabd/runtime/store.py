"""PostgreSQL state of the chat: demo sessions, conversations, messages, runs, events.

Runs are a durable queue. A worker claims the oldest pending run of a
conversation while holding that conversation's row lock, so one conversation
never has two active runs, and renews a lease while it works. A run whose
lease expired (its worker died) is claimed again, up to ``max_attempts``.

Events get a per-conversation sequence number in the same transaction that
increments ``conversations.last_sequence``, and every append is announced
with ``NOTIFY nabd_events`` so SSE streams in any process wake up.

Case work (Phase 3) adds each run's verified complaint plan, its durable case
commands (stored before they are sent), the cases each conversation touched,
and a per-customer advisory lock that serializes case mutations.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class NewCommand:
    """A case command to store before it is sent."""

    operation_id: str
    position: str
    issue_ref: str
    operation: str
    request: dict[str, Any]
    card: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandRow:
    operation_id: str
    run_id: str
    conversation_id: str
    customer_id: str
    position: str
    issue_ref: str
    operation: str
    request: dict[str, Any]
    card: dict[str, Any]
    status: str
    attempts: int
    result: dict[str, Any] | None
    error_code: str | None

    @property
    def final(self) -> bool:
        return self.status in ("confirmed", "conflict", "failed")


def _command(row: dict[str, Any]) -> CommandRow:
    return CommandRow(
        operation_id=row["operation_id"], run_id=str(row["run_id"]), conversation_id=str(row["conversation_id"]),
        customer_id=row["customer_id"], position=row["position"], issue_ref=row["issue_ref"],
        operation=row["operation"], request=row["request"], card=row["card"], status=row["status"],
        attempts=row["attempts"], result=row["result"], error_code=row["error_code"])


CASE_LOCK_POLL_SECONDS = 0.2


class Store:
    def __init__(self, pool: AsyncConnectionPool, *, local_notify=None, lock_conninfo: str | None = None):
        self.pool = pool
        self._local_notify = local_notify  # in-process shortcut; NOTIFY reaches other processes
        # Case locks hold a connection for the whole complaint stage; a dedicated one keeps the pool free.
        self._lock_conninfo = lock_conninfo

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

    # --- Case work (Phase 3) -------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def case_lock(self, customer_id: str,
                        on_wait: Callable[[], Awaitable[None]] | None = None) -> AsyncIterator[bool]:
        """Serialize case mutations of one customer across runs and processes.

        A PostgreSQL session advisory lock: released on exit, or by PostgreSQL
        itself if the process dies. Yields whether the caller had to wait.
        """
        key = f"nabd-cases:{customer_id}"
        async with contextlib.AsyncExitStack() as stack:
            if self._lock_conninfo:
                conn = await AsyncConnection.connect(self._lock_conninfo, autocommit=True)
                stack.push_async_callback(conn.close)
            else:
                conn = await stack.enter_async_context(self.pool.connection())
            waited = False
            while True:
                cursor = await conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,))
                row = await cursor.fetchone()
                if (row[0] if isinstance(row, (tuple, list)) else next(iter(row.values()))):
                    break
                if not waited and on_wait is not None:
                    await on_wait()
                waited = True
                await asyncio.sleep(CASE_LOCK_POLL_SECONDS)
            try:
                yield waited
            finally:
                with contextlib.suppress(Exception):
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    async def recent_messages(self, conversation_id: str, message_id: str, limit: int) -> list[dict[str, Any]]:
        """The conversation up to and including this customer message, oldest first."""
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT m.id, m.role, m.text, m.created_at FROM nabd.messages m "
                "JOIN nabd.runs r ON r.id = m.run_id WHERE m.conversation_id = %s "
                "AND r.turn <= (SELECT turn FROM nabd.runs WHERE message_id = %s) "
                "ORDER BY r.turn DESC, (m.role = 'assistant') DESC LIMIT %s",
                (conversation_id, message_id, limit))).fetchall()
        return [{"id": str(row["id"]), "role": row["role"], "text": row["text"], "at": row["created_at"]}
                for row in reversed(rows)]

    async def staff_history(self, conversation_id: str) -> list[dict[str, Any]]:
        from nabd.runtime.staff_store import StaffStore
        return await StaffStore(self).history(conversation_id)

    async def case_plan(self, run_id: str) -> tuple[dict[str, Any] | None, list[CommandRow]]:
        """A run's stored complaint assessment and commands, if an earlier attempt saved them."""
        async with self.pool.connection() as conn:
            plan = await (await conn.execute(
                "SELECT assessment FROM nabd.run_complaints WHERE run_id = %s", (run_id,))).fetchone()
            rows = await (await conn.execute(
                "SELECT * FROM nabd.case_commands WHERE run_id = %s ORDER BY created_at, position",
                (run_id,))).fetchall()
        return (plan["assessment"] if plan else None), [_command(row) for row in rows]

    async def save_case_plan(self, run_id: str, conversation_id: str, customer_id: str,
                             assessment: dict[str, Any], commands: list[NewCommand]) -> bool:
        """Store the plan and all its commands in one transaction. False if one was already saved."""
        async with self.pool.connection() as conn, conn.transaction():
            saved = await (await conn.execute(
                "INSERT INTO nabd.run_complaints (run_id, conversation_id, customer_id, assessment) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                (run_id, conversation_id, customer_id, Jsonb(assessment)))).fetchone()
            if saved is None:
                return False
            for command in commands:
                await self._insert_command(conn, run_id, conversation_id, customer_id, command)
        return True

    async def add_command(self, run_id: str, conversation_id: str, customer_id: str, command: NewCommand) -> None:
        async with self.pool.connection() as conn:
            await self._insert_command(conn, run_id, conversation_id, customer_id, command)

    @staticmethod
    async def _insert_command(conn: AsyncConnection, run_id: str, conversation_id: str, customer_id: str,
                              command: NewCommand) -> None:
        await conn.execute(
            "INSERT INTO nabd.case_commands (operation_id, run_id, conversation_id, customer_id, position, "
            "issue_ref, operation, request, card, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
            "ON CONFLICT (operation_id) DO NOTHING",
            (command.operation_id, run_id, conversation_id, customer_id, command.position, command.issue_ref,
             command.operation, Jsonb(command.request), Jsonb(command.card)))

    async def mark_command(self, operation_id: str, status: str, *, result: dict[str, Any] | None = None,
                           error: str | None = None, sent: bool = False) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE nabd.case_commands SET status = %s, result = coalesce(%s, result), error_code = %s, "
                "attempts = attempts + %s, updated_at = now(), reconcile_lease_until = NULL "
                "WHERE operation_id = %s",
                (status, Jsonb(result) if result is not None else None, error, 1 if sent else 0, operation_id))

    async def record_case(self, conversation_id: str, customer_id: str, run_id: str, *, case_id: str,
                          case_number: str, department: str | None, status: str | None, operation: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            # Same lock order as staff projection, translation and event append.
            await conn.execute("SELECT 1 FROM nabd.conversations WHERE id = %s FOR UPDATE", (conversation_id,))
            await conn.execute(
                "INSERT INTO nabd.issue_links (conversation_id, case_id, customer_id, case_number, department, "
                "status, last_operation, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (conversation_id, case_id) DO UPDATE SET case_number = excluded.case_number, "
                "department = coalesce(excluded.department, nabd.issue_links.department), "
                "status = excluded.status, last_operation = excluded.last_operation, run_id = excluded.run_id, "
                "removed_at = NULL, updated_at = now()",
                (conversation_id, case_id, customer_id, case_number, department, status, operation, run_id))
            from nabd.runtime.staff_store import StaffStore
            await StaffStore._queue(conn, case_id, "command")

    async def claim_reconcile(self, *, older_than_seconds: float, lease_seconds: float, max_attempts: int,
                              limit: int = 10) -> tuple[list[CommandRow], list[CommandRow]]:
        """(to reconcile, given up): commands of finished runs whose outcome is still open.

        Sent commands with an unknown outcome are leased to this caller. Commands
        never sent are abandoned (they had no effect), and commands that used up
        their attempts are failed; both come back as given up, so their cards close.
        """
        async with self.pool.connection() as conn, conn.transaction():
            closed = await (await conn.execute(
                "UPDATE nabd.case_commands c SET status = 'failed', updated_at = now(), "
                "error_code = CASE WHEN c.status = 'pending' THEN 'abandoned' ELSE 'reconcile_exhausted' END "
                "FROM nabd.runs r WHERE r.id = c.run_id AND r.status IN ('completed', 'failed') "
                "AND (c.status = 'pending' OR (c.status IN ('dispatched', 'unknown') AND c.attempts >= %s)) "
                "RETURNING c.*", (max_attempts,))).fetchall()
            rows = await (await conn.execute(
                "UPDATE nabd.case_commands SET reconcile_lease_until = now() + %s WHERE operation_id IN ("
                "  SELECT c.operation_id FROM nabd.case_commands c JOIN nabd.runs r ON r.id = c.run_id "
                "  WHERE c.status IN ('dispatched', 'unknown') AND r.status IN ('completed', 'failed') "
                "  AND c.updated_at < now() - %s "
                "  AND (c.reconcile_lease_until IS NULL OR c.reconcile_lease_until < now()) "
                "  ORDER BY c.updated_at LIMIT %s FOR UPDATE OF c SKIP LOCKED) RETURNING *",
                (timedelta(seconds=lease_seconds), timedelta(seconds=older_than_seconds), limit))).fetchall()
        return [_command(row) for row in rows], [_command(row) for row in closed]

    # --- Snapshot ----------------------------------------------------------------------------

    async def snapshot(self, conversation_id: str) -> dict[str, Any]:
        """Current state for a (re)loading browser: messages, runs, their activity and partial replies."""
        async with self.pool.connection() as conn, conn.transaction():
            conversation = await (await conn.execute(
                "SELECT last_sequence FROM nabd.conversations WHERE id = %s FOR UPDATE", (conversation_id,))).fetchone()
            messages = await (await conn.execute(
                "SELECT m.id, m.role, m.run_id, m.text, m.created_at FROM nabd.messages m "
                "JOIN nabd.runs r ON r.id = m.run_id WHERE m.conversation_id = %s "
                "ORDER BY r.turn, m.role = 'assistant'", (conversation_id,))).fetchall()
            runs = await (await conn.execute(
                "SELECT id, message_id, turn, status, attempt, error_code, created_at, started_at, finished_at "
                "FROM nabd.runs WHERE conversation_id = %s ORDER BY turn DESC LIMIT %s",
                (conversation_id, SNAPSHOT_RUNS))).fetchall()
            run_ids = [row["id"] for row in runs]
            language = await (await conn.execute(
                "SELECT payload FROM nabd.events WHERE conversation_id = %s AND type = 'conversation.language' "
                "AND sequence <= %s ORDER BY sequence DESC LIMIT 1",
                (conversation_id, conversation["last_sequence"]))).fetchone()
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
            cases = await (await conn.execute(
                "SELECT case_number, department, status, last_operation, updated_at FROM nabd.issue_links "
                "WHERE conversation_id = %s AND removed_at IS NULL ORDER BY created_at", (conversation_id,))).fetchall()
            replies = await (await conn.execute(
                "SELECT * FROM nabd.staff_replies WHERE conversation_id = %s AND withdrawn_at IS NULL "
                "ORDER BY created_at", (conversation_id,))).fetchall()
            notices = await (await conn.execute(
                "SELECT * FROM nabd.events WHERE conversation_id = %s "
                "AND type IN ('case.staff_update', 'case.removed') AND sequence <= %s ORDER BY sequence",
                (conversation_id, conversation["last_sequence"]))).fetchall()
        from nabd.runtime.staff_projection import reply_payload
        staff_replies = []
        for r in replies:
            item = reply_payload(str(r["id"]), r["case_number"], r["body"], r["posted_at"], r["revision"],
                                 r["translation_status"], r["translation"])
            item["after_message_id"] = str(r["after_message_id"]) if r["after_message_id"] else None
            staff_replies.append(item)
        attempts = {str(row["id"]): row["attempt"] for row in runs}
        partial: dict[str, dict[str, Any]] = {}
        for row in deltas:
            payload = row["payload"]
            if payload.get("attempt") != attempts.get(str(row["run_id"])):
                continue  # text of an earlier, abandoned attempt
            item = partial.setdefault(payload["message_id"], {"run_id": str(row["run_id"]), "text": ""})
            item["text"] += payload["text"]
        return {
            "staff_replies": staff_replies,
            "staff_notices": [_event_dict(row) for row in notices],
            "language": language["payload"] if language else None,
            "last_sequence": conversation["last_sequence"],
            "messages": [{"id": str(m["id"]), "role": m["role"], "run_id": _id(m["run_id"]), "text": m["text"],
                          "created_at": m["created_at"].isoformat()} for m in messages],
            "runs": [{"id": str(r["id"]), "message_id": str(r["message_id"]), "turn": r["turn"],
                      "status": r["status"], "attempt": r["attempt"], "error": r["error_code"],
                      "created_at": r["created_at"].isoformat()} for r in reversed(runs)],
            "events": [_event_dict(row) for row in events],
            "partial": partial,
            "cases": [{"case_number": c["case_number"], "department": c["department"], "status": c["status"],
                       "last_operation": c["last_operation"], "updated_at": c["updated_at"].isoformat()}
                      for c in cases],
        }
