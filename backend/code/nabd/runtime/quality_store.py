"""Private PostgreSQL records and fenced leases for the quality worker."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from nabd.runtime.events import RunContext
from nabd.runtime.store import Store

Queue = Literal["quality_reviews", "summary_revisions"]


async def save_trace(conn: AsyncConnection, run_id: str, trace: RunContext) -> None:
    items = trace.trace or [{"agent": "", "kind": "attempt", "name": "empty", "arguments": None,
                             "result": None, "usage": None, "duration_ms": None}]
    partial = trace.attempt > 1 or trace.trace_partial or not trace.trace
    async with conn.cursor() as cursor:
        await cursor.executemany("""
            INSERT INTO nabd.run_traces
                (run_id, attempt, position, partial, agent, kind, name, arguments, result, usage, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, [(run_id, trace.attempt, position, partial, item["agent"], item["kind"], item["name"],
                   Jsonb(item["arguments"]), Jsonb(item["result"]), Jsonb(item["usage"]), item["duration_ms"])
                  for position, item in enumerate(items)])


def _queue(queue: Queue) -> tuple[sql.Identifier, sql.Identifier]:
    return sql.Identifier("nabd", queue), sql.Identifier("run_id" if queue == "quality_reviews" else "operation_id")


class QualityStore:
    def __init__(self, store: Store):
        self.store = store
        self.pool = store.pool

    async def claim(self, queue: Queue, *, lease: float, max_attempts: int) -> dict | None:
        table, key = _queue(queue)
        # An already dispatched revision gets one final receipt-only attempt.
        limit = sql.SQL("(%s + dispatched::int)") if queue == "summary_revisions" else sql.SQL("%s")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(sql.SQL("""
                UPDATE {} SET status = 'failed', error_code = 'attempts_exhausted',
                    updated_at = now(), lease_token = NULL, lease_until = NULL
                WHERE status = 'pending' AND attempts >= {} AND (lease_until IS NULL OR lease_until <= now())
                """).format(table, limit), (max_attempts,))
            return await (await conn.execute(sql.SQL("""
                WITH due AS (
                    SELECT {key} FROM {table}
                    WHERE status = 'pending' AND not_before <= now() AND attempts < {limit}
                      AND (lease_until IS NULL OR lease_until <= now())
                    ORDER BY not_before, {key} LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE {table} AS job SET attempts = attempts + 1, lease_token = %s, lease_until = now() + %s
                FROM due WHERE job.{key} = due.{key} RETURNING job.*
                """).format(table=table, key=key, limit=limit),
                (max_attempts, uuid.uuid4(), timedelta(seconds=lease)))).fetchone()

    async def retry(self, queue: Queue, job: dict, error: str, max_attempts: int) -> None:
        table, key = _queue(queue)
        async with self.pool.connection() as conn:
            await conn.execute(sql.SQL("""
                UPDATE {} SET status = CASE WHEN attempts >= {} THEN 'failed' ELSE 'pending' END,
                    lease_token = NULL, lease_until = NULL, error_code = %s,
                    not_before = now() + %s, updated_at = now()
                WHERE {} = %s AND lease_token = %s AND lease_until > now()
                """).format(table, sql.SQL("(%s + dispatched::int)" if queue == "summary_revisions" else "%s"), key),
                (max_attempts, error, timedelta(seconds=min(60, 2 ** job["attempts"])),
                 job["run_id" if queue == "quality_reviews" else "operation_id"], job["lease_token"]))

    async def trace(self, run_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            return await (await conn.execute("""
                SELECT attempt, position, partial, agent, kind, name, arguments, result, usage, duration_ms
                FROM nabd.run_traces WHERE run_id = %s ORDER BY attempt, position
                """, (run_id,))).fetchall()

    async def load_review(self, run_id: str, history_limit: int) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            turn = await (await conn.execute("""
                SELECT r.message_id, r.conversation_id, customer.text AS customer, reply.text AS reply, reply.id AS reply_id
                FROM nabd.runs r JOIN nabd.messages customer ON customer.id = r.message_id
                JOIN nabd.messages reply ON reply.run_id = r.id AND reply.role = 'assistant'
                WHERE r.id = %s AND r.status = 'completed'
                """, (run_id,))).fetchone()
            command_rows = await (await conn.execute("""
                SELECT c.operation_id, c.operation, c.request, c.result
                FROM nabd.case_commands c JOIN nabd.runs r ON c.run_id = r.id
                WHERE c.run_id = %s AND c.status = 'confirmed' AND c.updated_at <= r.finished_at
                ORDER BY c.position
                """, (run_id,))).fetchall()
        plan, _ = await self.store.case_plan(run_id)
        history = await self.store.recent_messages(str(turn["conversation_id"]), str(turn["message_id"]),
                                                   history_limit + 1)
        turn_messages = {str(turn["message_id"]), str(turn["reply_id"])}
        history = [line for line in history if line["id"] not in turn_messages]
        trace = await self.trace(run_id)
        from nabd.agents.quality import review_input
        return review_input(turn["customer"], turn["reply"], history, plan, command_rows, trace)

    async def grade(self, job: dict, grade: dict, usage: dict, revisions: list[dict]) -> bool:
        async with self.pool.connection() as conn, conn.transaction():
            saved = await conn.execute("""
                UPDATE nabd.quality_reviews SET status = 'graded', grade = %s, usage = %s,
                    lease_token = NULL, lease_until = NULL, error_code = NULL, updated_at = now()
                WHERE run_id = %s AND lease_token = %s AND lease_until > now()
                """, (Jsonb(grade), Jsonb(usage), job["run_id"], job["lease_token"]))
            if not saved.rowcount:
                return False
            for revision in revisions:
                await conn.execute("""
                    INSERT INTO nabd.summary_revisions (operation_id, source_command, case_id, assessment)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (source_command) DO NOTHING
                    """, (revision["operation_id"], revision["source_command"], revision["case_id"],
                          Jsonb(revision["assessment"])))
        return True

    async def revision_context(self, job: dict) -> dict:
        async with self.pool.connection() as conn:
            return await (await conn.execute("""
                SELECT operation_id, customer_id, conversation_id, run_id, request, result
                FROM nabd.case_commands WHERE operation_id = %s AND status = 'confirmed'
                """, (job["source_command"],))).fetchone()

    async def prepare(self, job: dict, request: dict, usage: dict) -> bool:
        async with self.pool.connection() as conn:
            result = await conn.execute("""
                UPDATE nabd.summary_revisions SET request = %s, usage = %s, updated_at = now()
                WHERE operation_id = %s AND lease_token = %s AND lease_until > now() AND request IS NULL
                """, (Jsonb(request), Jsonb(usage), job["operation_id"], job["lease_token"]))
        return result.rowcount == 1

    async def dispatch(self, job: dict) -> bool:
        async with self.pool.connection() as conn:
            result = await conn.execute("""
                UPDATE nabd.summary_revisions SET dispatched = true, updated_at = now()
                WHERE operation_id = %s AND lease_token = %s AND lease_until > now() AND request IS NOT NULL
                """, (job["operation_id"], job["lease_token"]))
        return result.rowcount == 1

    async def finish_revision(self, job: dict, status: str, *, error: str | None = None,
                              result: dict | None = None, usage: dict | None = None) -> bool:
        async with self.pool.connection() as conn:
            updated = await conn.execute("""
                UPDATE nabd.summary_revisions SET status = %s, result = %s, error_code = %s,
                    usage = coalesce(%s, usage), lease_token = NULL, lease_until = NULL, updated_at = now()
                WHERE operation_id = %s AND lease_token = %s AND lease_until > now()
                """, (status, Jsonb(result), error, Jsonb(usage) if usage else None,
                      job["operation_id"], job["lease_token"]))
        return updated.rowcount == 1
