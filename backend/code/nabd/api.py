"""NABD chat HTTP and SSE endpoints (Phase 2).

The session cookie, set by demo sign-in, is the only source of the customer's
identity. Every conversation route checks ownership; another customer's
conversation answers exactly like a missing one. State-changing requests must
come from an allowed browser origin (the cookie is also SameSite=Lax).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from nabd.runtime.store import DemoSession, Store
from nabd.runtime.worker import EventNotifier, RunWorker
from nabd.salesforce.rest import SalesforceError
from nabd.salesforce.soql import CUSTOMER_ID
from nabd.settings import ChatSettings

logger = logging.getLogger(__name__)

COOKIE = "nabd_session"
POLL_SECONDS = 5.0  # fallback when a notification is missed; also the heartbeat interval
SESSION_CHECK_EVERY = 3  # idle polls between session validity checks
EVENT_PAGE = 500


@dataclass
class ChatRuntime:
    store: Store
    notifier: EventNotifier
    worker: RunWorker
    reads: Any  # SalesforceReads (only list_demo_customers/get_customer are used here)
    settings: ChatSettings
    tasks: list[asyncio.Task] = field(default_factory=list)


class SignIn(BaseModel):
    customer_id: str = Field(pattern=CUSTOMER_ID.pattern)


class NewMessage(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_\-:.]+$")
    text: str = Field(min_length=1)


def chat_runtime(request: Request) -> ChatRuntime:
    runtime = getattr(request.app.state, "chat", None)
    if runtime is None:
        raise HTTPException(503, detail="chat_not_configured")
    return runtime


def same_origin(request: Request, runtime: ChatRuntime = Depends(chat_runtime)) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in runtime.settings.allowed_origin_list:
        raise HTTPException(403, detail="origin_not_allowed")


async def signed_in(request: Request, runtime: ChatRuntime = Depends(chat_runtime)) -> DemoSession:
    session = await runtime.store.get_session(request.cookies.get(COOKIE))
    if session is None:
        raise HTTPException(401, detail="not_signed_in")
    return session


async def owned(conversation_id: str, session: DemoSession = Depends(signed_in),
                runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    conversation = await runtime.store.owns(session.customer_id, conversation_id)
    if conversation is None:
        raise HTTPException(404, detail="conversation_not_found")
    return conversation


def _session_body(session: DemoSession) -> dict[str, Any]:
    return {"customer_id": session.customer_id, "first_name": session.first_name,
            "display_name": session.display_name, "expires_at": session.expires_at.isoformat()}


router = APIRouter(prefix="/api")


@router.get("/demo/customers")
async def demo_customers(runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    if not runtime.settings.demo_sign_in:
        raise HTTPException(404, detail="demo_sign_in_disabled")
    try:
        customers = await runtime.reads.list_demo_customers()
    except SalesforceError:
        raise HTTPException(503, detail="salesforce_unavailable") from None
    return {"customers": [c.model_dump() for c in customers]}


@router.post("/demo/session", dependencies=[Depends(same_origin)])
async def sign_in(body: SignIn, response: Response, runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    if not runtime.settings.demo_sign_in:
        raise HTTPException(404, detail="demo_sign_in_disabled")
    try:
        customer = await runtime.reads.get_customer(body.customer_id)
    except SalesforceError:
        raise HTTPException(503, detail="salesforce_unavailable") from None
    if customer is None:
        raise HTTPException(404, detail="customer_not_found")
    first = customer.first_name or customer.last_name
    token, expires = await runtime.store.create_session(
        customer.customer_id, first, f"{customer.first_name or ''} {customer.last_name}".strip(),
        runtime.settings.session_hours)
    response.set_cookie(COOKIE, token, max_age=int(runtime.settings.session_hours * 3600), httponly=True,
                        samesite="lax", secure=runtime.settings.cookie_secure, path="/")
    session = DemoSession(customer.customer_id, first, f"{customer.first_name or ''} {customer.last_name}".strip(),
                          expires)
    return _session_body(session)


@router.get("/demo/session")
async def current_session(session: DemoSession = Depends(signed_in)) -> dict[str, Any]:
    return _session_body(session)


@router.delete("/demo/session", dependencies=[Depends(same_origin)], status_code=204)
async def sign_out(request: Request, runtime: ChatRuntime = Depends(chat_runtime)) -> Response:
    await runtime.store.end_session(request.cookies.get(COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=runtime.settings.cookie_secure)
    return response


@router.get("/conversations")
async def conversations(session: DemoSession = Depends(signed_in),
                        runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    return {"conversations": await runtime.store.list_conversations(session.customer_id)}


@router.post("/conversations", dependencies=[Depends(same_origin)], status_code=201)
async def create_conversation(session: DemoSession = Depends(signed_in),
                              runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    return await runtime.store.create_conversation(session.customer_id, session.first_name)


@router.get("/conversations/{conversation_id}")
async def conversation(conversation: dict[str, Any] = Depends(owned),
                       runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    return {"conversation": conversation, **await runtime.store.snapshot(conversation["id"])}


@router.post("/conversations/{conversation_id}/messages", dependencies=[Depends(same_origin)], status_code=202)
async def send_message(body: NewMessage, response: Response, conversation: dict[str, Any] = Depends(owned),
                       runtime: ChatRuntime = Depends(chat_runtime)) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(422, detail="empty_message")
    if len(text) > runtime.settings.max_message_chars:
        raise HTTPException(413, detail="message_too_long")
    accepted, created = await runtime.store.submit_message(conversation["id"], body.client_message_id, text)
    if not created:
        response.status_code = 200  # the same client message was already accepted
    else:
        runtime.worker.wake()
    return accepted


def _frame(event: dict[str, Any]) -> str:
    data = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event['sequence']}\nevent: {event['type']}\ndata: {data}\n\n"


@router.get("/conversations/{conversation_id}/events")
async def events(request: Request, after: int | None = None, conversation: dict[str, Any] = Depends(owned),
                 runtime: ChatRuntime = Depends(chat_runtime)) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    try:
        last = int(header) if header else (after or 0)
    except ValueError:
        raise HTTPException(400, detail="invalid_last_event_id") from None
    conversation_id = conversation["id"]
    cookie = request.cookies.get(COOKIE)

    async def stream() -> AsyncIterator[str]:
        nonlocal last
        yield "retry: 2000\n\n"
        idle = 0
        with runtime.notifier.subscribe(conversation_id) as wake:
            while True:
                wake.clear()
                batch = await runtime.store.events_after(conversation_id, last, EVENT_PAGE)
                for event in batch:
                    last = event["sequence"]
                    yield _frame(event)
                if len(batch) == EVENT_PAGE:
                    continue
                if await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(wake.wait(), POLL_SECONDS)
                    idle = 0
                except TimeoutError:
                    idle += 1
                    if idle % SESSION_CHECK_EVERY == 0 and await runtime.store.get_session(cookie) is None:
                        yield "event: session.ended\ndata: {}\n\n"
                        return
                    # A heartbeat without an id: browsers detect half-open proxied streams by its absence.
                    yield "event: ping\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
