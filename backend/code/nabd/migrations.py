"""Explicit, serialized application and checkpointer schema upgrades."""
from __future__ import annotations

import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import AsyncConnection

from nabd.database import open_database
from nabd.settings import BACKEND_ROOT, Settings

# PostgreSQL session advisory lock, shared by all NABD migration invocations.
MIGRATION_LOCK = 0x4E414244


def alembic_config(settings: Settings | None = None) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    if settings is not None:
        config.attributes["database_url"] = settings.sqlalchemy_url
    return config


def current_revision() -> str:
    revision = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert revision is not None, "NABD requires an initial migration"
    return revision


async def migrate(settings: Settings) -> None:
    async with await AsyncConnection.connect(
        settings.database_url.get_secret_value(),
        autocommit=True,
        connect_timeout=max(1, int(settings.database_timeout_seconds)),
    ) as lock:
        # A blocking SELECT pg_advisory_lock keeps a transaction snapshot alive
        # while waiting. That can deadlock CREATE INDEX CONCURRENTLY inside the
        # checkpointer's setup. Each try finishes its autocommit statement first.
        async with asyncio.timeout(60):
            while True:
                cursor = await lock.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK,))
                row = await cursor.fetchone()
                if row and row[0]:
                    break
                await asyncio.sleep(0.1)
        try:
            await asyncio.to_thread(command.upgrade, alembic_config(settings), "head")
            async with open_database(settings) as database:
                await database.checkpointer.setup()
        finally:
            await lock.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
