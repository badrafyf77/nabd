"""Runtime connection ownership and the real PostgreSQL LangGraph checkpointer."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from nabd.settings import Settings


@dataclass
class Database:
    pool: AsyncConnectionPool
    checkpointer: AsyncPostgresSaver
    timeout_seconds: float

    async def ready(self, expected_revision: str) -> bool:
        """Check connectivity and both migration systems, with a bounded deadline."""
        async with asyncio.timeout(self.timeout_seconds):
            async with self.pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT to_regclass('public.alembic_version') AS app, "
                    "to_regclass('nabd.checkpoint_migrations') AS migrations, "
                    "to_regclass('nabd.checkpoints') AS checkpoints, "
                    "to_regclass('nabd.checkpoint_blobs') AS blobs, "
                    "to_regclass('nabd.checkpoint_writes') AS writes"
                )).fetchone()
                if not row or not all(row.values()):
                    return False
                app = await (await conn.execute(
                    "SELECT version_num FROM public.alembic_version"
                )).fetchone()
                checkpoint = await (await conn.execute(
                    "SELECT max(v) AS version FROM nabd.checkpoint_migrations"
                )).fetchone()
                return bool(
                    app and app["version_num"] == expected_revision
                    and checkpoint
                    and checkpoint["version"] == len(AsyncPostgresSaver.MIGRATIONS) - 1
                )


@asynccontextmanager
async def open_database(settings: Settings) -> AsyncIterator[Database]:
    async with AsyncConnectionPool(
        conninfo=settings.database_url.get_secret_value(),
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        timeout=settings.database_timeout_seconds,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": max(1, int(settings.database_timeout_seconds)),
            "options": "-c search_path=nabd,public",
        },
        open=False,
    ) as pool:
        # Startup is independent of database availability; /readyz gates traffic.
        yield Database(
            pool=pool,
            checkpointer=AsyncPostgresSaver(
                pool, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
            ),
            timeout_seconds=settings.database_timeout_seconds,
        )
