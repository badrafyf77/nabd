"""CPU-only FastAPI entry point. Start with uvicorn nabd.app:app --app-dir code."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from nabd.database import open_database
from nabd.migrations import current_revision
from nabd.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        app.state.schema_revision = current_revision()
        async with open_database(config) as database:
            app.state.database = database
            yield

    application = FastAPI(title="NABD API", version="0.0.1", lifespan=lifespan)

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
