"""Real PostgreSQL checks, opted in via a separate disposable test database."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import make_url

from nabd.app import create_app
from nabd.database import open_database
from nabd.migrations import current_revision, migrate
from nabd.settings import Settings

pytestmark = pytest.mark.postgres
BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def empty_database():
    url = os.environ.get("NABD_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set NABD_TEST_DATABASE_URL to run real PostgreSQL checks")
    parsed = make_url(url)
    if not parsed.database or not parsed.database.endswith("_test"):
        pytest.fail("NABD_TEST_DATABASE_URL must name a disposable database ending in _test")
    name = f"nabd_{uuid4().hex}_test"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield Settings(
                database_url=parsed.set(database=name).render_as_string(hide_password=False),
                _env_file=None,
            )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_migrations_are_explicit_repeatable_and_gate_readiness(empty_database):
    with TestClient(create_app(empty_database)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        with psycopg.connect(empty_database.database_url.get_secret_value()) as conn:
            assert conn.execute("SELECT to_regnamespace('nabd')").fetchone() == (None,)
        asyncio.run(migrate(empty_database))
        asyncio.run(migrate(empty_database))
        assert client.get("/readyz").json() == {"service": "nabd", "status": "ready"}


def test_concurrent_migration_runners_serialize(empty_database):
    async def run():
        async with asyncio.timeout(15):
            await asyncio.gather(migrate(empty_database), migrate(empty_database))
        async with open_database(empty_database) as database:
            assert await database.ready(current_revision())
    asyncio.run(run())


@pytest.mark.parametrize("table", ["checkpoints", "checkpoint_blobs", "checkpoint_writes"])
def test_missing_checkpoint_tables_make_app_unready(empty_database, table):
    asyncio.run(migrate(empty_database))
    with TestClient(create_app(empty_database)) as client:
        assert client.get("/readyz").status_code == 200
        with psycopg.connect(empty_database.database_url.get_secret_value(), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP TABLE nabd.{}").format(sql.Identifier(table)))
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_graph_checkpoint_resumes_after_backend_process_restart(empty_database):
    asyncio.run(migrate(empty_database))
    script = """
import asyncio, json, sys
from langchain_core.messages import HumanMessage
from langgraph.graph import START, END, MessagesState, StateGraph
from nabd.app import create_app
from nabd.settings import Settings
class State(MessagesState):
    count: int
async def run():
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        builder = StateGraph(State)
        builder.add_node('count', lambda state: {'count': state.get('count', 0) + 1})
        builder.add_edge(START, 'count')
        builder.add_edge('count', END)
        graph = builder.compile(checkpointer=app.state.database.checkpointer)
        config = {'configurable': {'thread_id': sys.argv[2]}}
        if sys.argv[1] == 'write':
            result = await graph.ainvoke({'messages': [HumanMessage('salam, fin wsel talabi?')]}, config)
        else:
            previous = await graph.aget_state(config)
            assert previous.values['count'] == 1
            assert previous.values['messages'][0].content == 'salam, fin wsel talabi?'
            result = await graph.ainvoke({'messages': [HumanMessage('Merci, continue.')]}, config)
        print(json.dumps({'count': result['count'], 'messages': len(result['messages'])}))
asyncio.run(run())
"""
    env = {
        **os.environ, "PYTHONPATH": str(BACKEND / "code"), "LANGSMITH_TRACING": "false",
        "NABD_DATABASE_URL": empty_database.database_url.get_secret_value(),
    }
    thread_id = str(uuid4())
    for action, expected in [("write", {"count": 1, "messages": 1}), ("resume", {"count": 2, "messages": 2})]:
        result = subprocess.run(
            [sys.executable, "-c", script, action, thread_id], env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == expected
