# Phase 2 — Support chat and real-time agent progress

**Status: implemented and verified without paid model calls (18 November
2026); the real-model English scenario run is waiting for the user's approval.**
Depends on completed [Phase 1](01-salesforce.md), checkpoint `1a04f52`. The
handoff at the end of this page records what was delivered and verified.

## Outcome

A signed-in demo customer chats in English with ShopEasy support in the Next.js
app. The Supervisor and Orders agents answer from Salesforce through an
authenticated read MCP server, and the customer sees what the agents are doing
while they work. Conversations, messages and activity events survive a browser
refresh and a backend restart.

Read [accepted decisions](../decisions.md) first. The [roadmap](../roadmap.md),
[architecture](../architecture.md) and [contracts](../contracts.md) give the
target; this page fixes Phase 2's concrete choices.

## Starting facts (checked 18 November 2026)

- Phase 1 org `nabd-dev` connected; `verify`/`smoke` commands available.
- The legacy Supervisor/Orders graphs are synchronous, voice-shaped, use SQLite
  tools and a phone-number identity gate. They stay as legacy code and tests;
  NABD gets its own async agents under `backend/code/nabd/agents/`.
- PostgreSQL holds only checkpoint tables. `mcp` is not yet a dependency.
- The frontend is a single voice page. `backend/.env.nabd` has no model key.

## Implementation sequence

1. Write this plan.
2. Salesforce: generate `All` list views for every custom object tab; add the
   integration-readable `Policy_Days__c` number to `KnowledgeEntry__c` and set
   it to 30 on the published `policy.return_window` revision, so the return
   window used by code comes from Salesforce. Regenerate, deploy, seed, verify.
3. Read MCP server with signed customer context (below), backed by the Phase 1
   `SalesforceReads` adapter.
4. NABD agents (Supervisor, Orders) using real MCP calls, typed findings, tool
   budgets, and execution hooks that write public activity events.
5. PostgreSQL schema for demo sessions, conversations, messages, runs and
   events; a durable run worker with leases and checkpoint resume.
6. HTTP and SSE endpoints from [contracts](../contracts.md), demo sign-in.
7. Next.js chat with sign-in picker, message stream and activity cards.
8. Tests, real-org MCP smoke, browser check, and (with the user's approval) a
   small paid scenario run. Update docs and create the local checkpoint.

## Technical defaults chosen for Phase 2

### Read MCP server

- Official Python SDK `mcp` 2.3.0 (`MCPServer`), Streamable HTTP, stateless
  with JSON responses. Separate process `python -m nabd read-mcp`
  (port 8091, path `/mcp`); a Compose service of the same name.
- The server alone talks to Salesforce for agent work. Each request carries a
  bearer **context token** minted by the API for one run: HMAC-SHA256 over
  `{aud: nabd-read-mcp, sub: customer ID, scope: nabd.read, conversation, run,
  exp}`, shared secret `NABD_MCP_CONTEXT_SECRET`, lifetime = turn deadline plus
  60 s. The SDK's bearer middleware rejects missing/invalid tokens; tools,
  resources and prompts read the customer only from the verified token. No tool
  accepts a customer ID argument.
- Tools (all read-only, all customer-scoped):

  | Tool | Returns |
  | --- | --- |
  | `get_customer_profile` | Name, city, preferred language, record ref |
  | `list_orders` | Recent orders with products, totals, delivery status/dates |
  | `get_order` | One order with items, delivery and a code-computed return check |
  | `list_cases` | The customer's case numbers, subjects, status, category |
  | `list_knowledge` | Index of current published FAQ/policy entries (key, title, question, revision) |
  | `get_knowledge` | Bodies of named published entries with revision key and record version |

- Knowledge is chosen by meaning from the index, not keyword search, so later
  languages need no routing rules. The return check uses
  `Policy_Days__c` of the published `policy.return_window` revision and cites
  that revision; a missing value makes the check unavailable rather than
  falling back to a constant.
- Every result carries evidence references `sobject:id@version`.
- Resources: `nabd://customer/profile`, `nabd://knowledge/index`,
  `nabd://knowledge/{entry_key}`. Prompt: `staff_review`, a customer-scoped
  review template (orders, cases and published policy references) for staff or
  a later quality reviewer; rendered by `python -m nabd read-mcp prompt`.

### Agents and turns

- Supervisor: only customer-facing agent; text chat in English; tools
  `get_customer_profile`, `list_cases`, `list_knowledge`, `get_knowledge` and
  `ask_orders_specialist`. No write tools exist yet, so it must say plainly
  that cases, refunds and order changes cannot be opened from chat yet.
- Orders specialist: `list_orders`, `get_order`; returns typed findings
  (`done`, `needs_information`, `cannot_complete`; facts with evidence; order
  numbers; open questions). Code keeps only facts whose evidence references were
  actually returned by its tools; others are marked unverified.
- The Supervisor awaits the specialist inside the turn (no background thread).
- Budgets: 5 tool calls per agent invocation, 20 shared per turn, 120 s turn
  deadline. Models default to `gpt-5.4-mini` (Responses API), key
  `NABD_OPENAI_API_KEY` passed explicitly, never read from the legacy `.env`.
- LangGraph `AsyncPostgresSaver` checkpoints the Supervisor per conversation.

### Durable runs and events

- Tables: `demo_sessions` (hashed token), `conversations`, `messages`
  (unique client message ID per conversation), `runs` (durable queue: queued,
  running, completed, failed; attempt, lease) and `events` (per-conversation
  sequence).
- The worker runs inside the API process, claims runs under a conversation row
  lock (one active run per conversation, oldest first), renews a 30 s lease and
  retries a crashed run up to 3 attempts. A retry resumes the checkpoint when
  the interrupted run left pending graph work, finalizes when the graph had
  already finished, and otherwise starts the turn.
- Event types: `message.created`, `message.delta` (coalesced), `message.completed`,
  `run.queued`, `run.started`, `run.retrying`, `run.completed`, `run.failed`,
  `activity`. Activity states: queued, running, waiting (with what it waits
  for), retrying, completed, failed. Stable activity IDs come from run and tool
  call IDs; texts are localization codes plus allowed parameters.
- Events are written only by execution hooks in code, contain only public
  projections, and are announced with PostgreSQL `NOTIFY`; SSE replays after
  `Last-Event-ID` and polls as a fallback.

### HTTP and browser

- Endpoints from the contracts plus `GET /api/demo/customers`,
  `GET /api/demo/session` and `GET /api/conversations` (needed to restore the
  chat). Session cookie `nabd_session`: HttpOnly, SameSite=Lax, 12 h.
  Another customer's conversation is indistinguishable from a missing one (404).
  State-changing requests must be JSON and from an allowed origin.
- Next.js rewrites `/api/*` to the API (same-origin path). The legacy voice page
  moves to `/voice`.

## Verification gate

- Offline: token signing/expiry/tampering; MCP tools, resources and prompt over
  real Streamable HTTP with fake Salesforce data; scoping and refusals;
  agents with the scripted fake model; budgets; findings verification.
- PostgreSQL: sign-in/session expiry, ownership, idempotent messages, queued
  extra turns, event replay, crash/lease recovery and checkpoint resume.
- Real org: `verify` with the new list views/field; MCP smoke as the
  integration user through the real server.
- Browser: sign in, ask questions, watch cards, refresh mid-run, restart API.
- Paid scenarios only after the user approves the run size.

## Phase boundary

Complaint analysis, case writes and the write MCP are Phase 3. Other languages,
RTL and localized templates beyond English are Phase 4. Staff synchronization
is Phase 5. Do not add them early.

## Handoff — 18 November 2026

Git checkpoint: `02178faa969f4747d58de98c0f1b1a043f9a9ad9` (*Build NABD support chat with live agent activity
(Phase 2)*) on `codex/nabd-chat-salesforce`.

### Delivered

- **Salesforce**: generated `All` list views on the five custom object tabs;
  `KnowledgeEntry__c.Policy_Days__c` (integration-readable), set to 30 on
  `policy.return_window@1`. The seed recognises fingerprints written before a
  field joined the mapping (`seed.ADDED_FIELDS`), so the new field did not make
  every revision look staff-edited. Deployed, seeded and verified (39 checks).
- **Read adapter**: product names in `list_orders`, `policy_days` on knowledge,
  `list_demo_customers` for the sign-in picker.
- **Signed context** (`nabd/context.py`): HMAC-SHA256 tokens per run naming
  customer, conversation, run, audience, scope and expiry.
- **Read MCP server** (`nabd/read_mcp/`): six read-only tools, three resources,
  the `staff_review` prompt, bearer-token auth through the SDK middleware,
  coded secret-free errors, DNS-rebinding host checks, `/healthz`.
  `python -m nabd read-mcp serve | prompt | smoke`; Compose service `read-mcp`.
- **Agents** (`nabd/agents/`): async Supervisor (checkpointed per conversation)
  and Orders specialist over real MCP calls; per-agent allowlists; 5/20 tool
  budgets; typed findings with evidence verification; English text-chat
  prompts with no write capabilities claimed.
- **Durable runtime** (`nabd/runtime/`, migration `0002_chat_runtime`): demo
  sessions (hashed tokens), conversations, idempotent messages, a run queue with
  conversation locks, leases, three attempts and checkpoint resume/finalize;
  per-conversation event log with `NOTIFY`; coalesced reply deltas.
- **HTTP/SSE** (`nabd/api.py`): the contract endpoints plus
  `GET /api/demo/customers`, `GET /api/demo/session`, `GET /api/conversations`;
  ownership as 404; origin check; SSE replay from `Last-Event-ID`/`after`, a
  5 s heartbeat, polling fallback and session-end notice.
- **Frontend** (`frontend/src/chat/`): sign-in picker, conversation list,
  streamed replies, nested activity cards (open while running, collapsed to a
  summary after), confirmed findings, sources with revisions, waiting/retry/
  failure states, queued turns, refresh restore, heartbeat-based reconnect.
  The voice prototype moved to `/voice`.

### Verification results

| Check | Result |
| --- | --- |
| Offline suite, macOS, real PostgreSQL | **213 passed**; 45 legacy and 12 new paid scenarios deselected |
| New offline tests | context tokens, MCP over Streamable HTTP (13), agents with scripted models (8), API under uvicorn with PostgreSQL (5: sign-in/ownership, streaming and replay, idempotency and queueing, deadline, restart resume) |
| Salesforce `verify` | **39/39** (new: All list views, published policy days) |
| Seed after the field addition / repeated | 24 knowledge revisions refreshed, none preserved; then **0 writes** |
| `salesforce smoke` | 12/12 |
| `read-mcp smoke` against the real org | **12/12**: no-context and expired tokens refused, tools listed, scoped profile/orders, other customer's order not found, published-only knowledge and current revision, scoped resource, return window from policy, public case fields, scoped prompt |
| Browser (scripted models, real API, MCP and Salesforce) | Sign-in from Salesforce, streamed reply, nested cards with findings, waiting state, queued second message, reload mid-run restored, backend killed mid-run → "Reconnecting…" → run resumed (attempt 2) and finished live |
| Frontend `tsc` and `next build` | Clean |
| Real-model English scenarios (`-m llm`, 12) | **Not run**: needs the user's approval and `NABD_OPENAI_API_KEY` |

No paid model calls were made in Phase 2. Linux image results are in
[status](../status.md).

### Findings worth keeping

- MCP Python SDK 2.x renamed `FastMCP` to `MCPServer`, uses snake_case model
  fields and `httpx2`. Tool errors arrive as "Error executing tool …: [code] …".
- Errors raised inside the MCP client session come back wrapped in an
  `ExceptionGroup`; the connector unwraps single errors.
- Streamable HTTP refuses unknown `Host` headers (421); Compose sets
  `NABD_READ_MCP_ALLOWED_HOSTS`.
- A browser stream proxied by Next.js stays half-open when the API dies; the
  heartbeat lets the page notice and reopen it. An EventSource that gets an
  HTTP error closes for good, so the page reopens it itself.
- FastAPI 0.163 lists included routers as one route without `.path`.

### Known limitations

- The real-model gate ("English support scenarios succeed through
  Salesforce/MCP") is prepared (`tests/nabd/test_chat_scenarios.py`) but not
  measured. Prompt quality with real models is therefore unverified.
- No write tools: cases, refunds, order changes and hand-offs cannot be done
  from chat yet (Phase 3); the Supervisor is told to say so.
- English only; activity texts are English templates keyed by stable codes.
- The run worker lives in the API process; one API process was exercised. The
  queue and `NOTIFY` design allows more, but that was not tested.
- Engineering traces are only logs (and LangSmith if enabled); no restricted
  developer view yet.
- The Phase 0 Compose API image (`nabd-backend:phase0`) still runs on port 8081
  against the dev database at revision 0001; rebuild and migrate to use Phase 2.

### Resume commands

From `backend/`, with `NABD_READ_MCP_CONTEXT_SECRET` and `NABD_OPENAI_API_KEY`
set in `.env.nabd`:

```bash
docker compose -f docker-compose.nabd.yml up -d --build --wait api
PYTHONPATH=code .venv-text/bin/python -m nabd read-mcp smoke
cd ../frontend && npm run dev   # http://localhost:3000
```

Paid check, only with the user's approval:

```bash
cd backend && .venv-text/bin/python -m pytest -m llm tests/nabd/test_chat_scenarios.py -s
```
