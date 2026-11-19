"""CPU-only FastAPI entry point. Start with uvicorn nabd.app:app --app-dir code.

Health and readiness work with storage alone. The chat also needs the read
and write MCP servers (and their context secrets), the OpenAI key and the
Salesforce integration settings; without them the chat routes answer 503
``chat_not_configured``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from nabd.agents.cases import ComplaintStage
from nabd.agents.mcp_tools import McpConnector, http_connector
from nabd.agents.models import AgentModels, build_models
from nabd.agents.supervisor import Supervisor
from nabd.api import ChatRuntime, router
from nabd.database import open_database
from nabd.migrations import current_revision
from nabd.runtime.staff_sync import StaffSync
from nabd.agents.staff_translation import StaffTranslator
from nabd.salesforce.pubsub import PubSubClient
from nabd.runtime.store import Store
from nabd.runtime.worker import EventNotifier, RunWorker, TurnExecutor
from nabd.salesforce.reads import SalesforceReads
from nabd.salesforce.rest import ClientCredentialsAuth, SalesforceClient
from nabd.settings import (
    BusinessSettings, ChatSettings, StaffSyncSettings, ModelSettings, ReadMcpSettings, SalesforceSettings, Settings, WriteMcpSettings,
)

logger = logging.getLogger(__name__)


@dataclass
class ChatDependencies:
    """Everything the chat needs from outside; tests pass fakes, production builds them."""

    reads: Any
    models: AgentModels
    connect: McpConnector
    read_mcp: ReadMcpSettings
    chat: ChatSettings
    business: BusinessSettings
    connect_writes: McpConnector
    write_mcp: WriteMcpSettings
    subscriber: Any = None
    staff_sync: StaffSyncSettings | None = None


async def _production_chat(stack: AsyncExitStack) -> ChatDependencies | None:
    try:
        read_mcp, models, salesforce = ReadMcpSettings(), ModelSettings(), SalesforceSettings()
        chat, business, write_mcp = ChatSettings(), BusinessSettings(), WriteMcpSettings()
    except ValidationError as error:
        missing = sorted({".".join(str(p) for p in item["loc"]) for item in error.errors()})
        logger.warning("NABD chat disabled; missing or invalid settings: %s", ", ".join(missing))
        return None
    http = await stack.enter_async_context(httpx.AsyncClient(timeout=salesforce.timeout_seconds))
    auth = ClientCredentialsAuth(salesforce.domain_url, salesforce.client_id, salesforce.client_secret, http)
    return ChatDependencies(
        reads=SalesforceReads(SalesforceClient(auth, http, salesforce.api_version)),
        models=build_models(models),
        connect=http_connector(read_mcp.url, read_mcp.timeout_seconds),
        read_mcp=read_mcp, chat=chat, business=business,
        connect_writes=http_connector(write_mcp.url, write_mcp.timeout_seconds), write_mcp=write_mcp,
        subscriber=PubSubClient(auth), staff_sync=StaffSyncSettings(),
    )


def create_app(settings: Settings | None = None, *, chat: ChatDependencies | None = None,
               start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        app.state.schema_revision = current_revision()
        app.state.chat = None
        async with open_database(config) as database, AsyncExitStack() as stack:
            app.state.database = database
            deps = chat if chat is not None else await _production_chat(stack)
            if deps is not None:
                notifier = EventNotifier(config.database_url.get_secret_value())
                store = Store(database.pool, local_notify=notifier.wake,
                              lock_conninfo=config.database_url.get_secret_value())
                stage = ComplaintStage(deps.models.complaints, store, deps.chat.review_confidence)
                executor = TurnExecutor(Supervisor(deps.models.supervisor, deps.models.orders,
                                                   database.checkpointer),
                                        deps.connect, deps.read_mcp, deps.chat, deps.business,
                                        stage, deps.connect_writes, deps.write_mcp)
                worker = RunWorker(store, executor, deps.chat)
                runtime = ChatRuntime(store, notifier, worker, deps.reads, deps.chat)
                runtime.tasks.append(asyncio.create_task(notifier.listen(), name="nabd-event-listener"))
                if start_worker:
                    runtime.tasks.append(asyncio.create_task(worker.run_forever(), name="nabd-run-worker"))
                if start_worker and deps.subscriber is not None and (deps.staff_sync or StaffSyncSettings()).enabled:
                    runtime.staff = StaffSync(store, deps.reads, deps.subscriber,
                                              StaffTranslator(deps.models.supervisor),
                                              deps.staff_sync or StaffSyncSettings())
                    runtime.tasks.append(asyncio.create_task(runtime.staff.run_forever(), name="nabd-staff-sync"))
                app.state.chat = runtime
            try:
                yield
            finally:
                if app.state.chat is not None:
                    for task in app.state.chat.tasks:
                        task.cancel()
                    for task in app.state.chat.tasks:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

    application = FastAPI(title="NABD API", version="0.3.0", lifespan=lifespan)
    application.include_router(router)

    @application.get("/healthz")
    async def health() -> dict[str, str]:
        return {"service": "nabd", "status": "ok"}

    @application.get("/readyz")
    async def readiness(request: Request) -> JSONResponse:
        try:
            ready = await request.app.state.database.ready(request.app.state.schema_revision)
        except (DatabaseError, PoolTimeout, TimeoutError):
            # Never include connection strings or driver error details in responses.
            logger.warning("NABD database readiness check failed")
            ready = False
        return JSONResponse(
            {"service": "nabd", "status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    return application


app = create_app()
