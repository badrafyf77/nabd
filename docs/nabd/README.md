# NABD development guide

NABD is ShopEasy's multilingual text support and complaint-intelligence product.
Salesforce owns business records and knowledge; PostgreSQL owns execution state.
The implementation progresses through phases 0–7. **For a new chat, start with
[START_HERE](START_HERE.md)** for the checkpoint, reading order, pitfalls, and
copyable continuation prompt. Current progress is in [status](status.md). Phase 1
delivered the [Salesforce business system](salesforce.md); Phase 2 the
[support chat](phases/02-support-chat.md); Phase 3 the
[complaint cases](phases/03-complaints.md) described below.

## Phase 0: run the text foundation

From the repository root:

```bash
cd backend
docker compose -f docker-compose.nabd.yml up -d --build --wait api
curl --fail http://localhost:8081/healthz
curl --fail http://localhost:8081/readyz
```

Compose starts PostgreSQL, runs migrations, and starts the CPU-only API.
Database data lives in the `nabd_nabd_postgres` named volume. API port: 8081;
database port: 55432, both bound to localhost. Development credentials in this
Compose file are fixtures for this local stack.

The only product endpoints implemented in Phase 0 are:

| Route | Healthy result | Failure behavior |
| --- | --- | --- |
| `GET /healthz` | `200`, service `nabd`, status `ok` | Independent of database availability |
| `GET /readyz` | `200`, service `nabd`, status `ready` | `503`, status `not_ready`, for unavailable/unmigrated storage |

Readiness checks the application migration revision and LangGraph migration
revision. The API never silently creates tables during startup. A missing
`NABD_DATABASE_URL` is a configuration error. Model/Salesforce credentials are
not required in Phase 0. Salesforce commands arrived in Phase 1 (below); chat arrives in Phase 2.

```bash
# Inspect services or rerun explicit migrations:
docker compose -f docker-compose.nabd.yml ps -a
docker compose -f docker-compose.nabd.yml logs --tail 50 api migrate
docker compose -f docker-compose.nabd.yml run --rm migrate

# Restart the API; database state remains:
docker compose -f docker-compose.nabd.yml restart api

# Stop the local stack while preserving its database volume:
docker compose -f docker-compose.nabd.yml --profile test down
```

## Local Python development

Use Python 3.14 and the isolated text environment:

```bash
cd backend
python3 -m venv .venv-text
.venv-text/bin/python -m pip install -r requirements-text-dev.txt
cp .env.nabd.example .env.nabd
docker compose -f docker-compose.nabd.yml up -d --wait db
PYTHONPATH=code .venv-text/bin/python -m nabd migrate
.venv-text/bin/python -m uvicorn nabd.app:app --app-dir code --port 8081
```

Stop the container API before using the same port for local Python. Settings
load `backend/.env.nabd` regardless of cwd; environment variables override it.
The legacy `backend/.env` is not loaded by NABD startup.

| Setting | Meaning | Default |
| --- | --- | --- |
| `NABD_DATABASE_URL` | PostgreSQL URI; required | None |
| `NABD_DATABASE_POOL_MIN_SIZE` | Minimum pooled connections | 1 |
| `NABD_DATABASE_POOL_MAX_SIZE` | Maximum pooled connections | 5 |
| `NABD_DATABASE_TIMEOUT_SECONDS` | Pool/readiness deadline | 3 seconds |

Settings reject malformed URLs and invalid pool bounds, and redact credentials
in validation errors and representations. Readiness responses contain no driver
errors or connection strings.

## Phase 1: Salesforce business system

Salesforce work needs the Salesforce CLI (`npm install --global @salesforce/cli`)
logged in to the dedicated org: `sf org login web --alias nabd-dev`. The full
reference (objects, access, fixtures, read adapter) is
[NABD Salesforce](salesforce.md). From `backend/`:

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce generate   # after editing schema.py
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce deploy --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce configure --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce seed --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce verify --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce smoke
```

A brand-new org needs `configure --create-integration-user` once; it creates
the API-only integration user. Then copy the `NABD_Backend` client app's
Consumer Key and Secret (Setup → External Client App Manager → NABD Backend →
Settings → OAuth Settings) into `backend/.env.nabd`. Re-run `configure` after
any change to `salesforce/integration-app`: deploying that policy clears the
app's approved permission set until `configure` grants it again.

`seed` never overwrites a record edited after the last seed and never deletes.
`reset --target-org nabd-dev --confirm nabd-dev` explicitly restores fixture
values. Deploy/configure/seed/verify use the developer's CLI session; only
`smoke` and backend code use the integration credentials below.

| Setting | Meaning | Default |
| --- | --- | --- |
| `NABD_SALESFORCE_DOMAIN_URL` | Org My Domain, `https://<domain>.my.salesforce.com` | None |
| `NABD_SALESFORCE_CLIENT_ID` | `NABD_Backend` consumer key (secret) | None |
| `NABD_SALESFORCE_CLIENT_SECRET` | `NABD_Backend` consumer secret (secret) | None |
| `NABD_SALESFORCE_API_VERSION` | REST API version | 68.0 |
| `NABD_SALESFORCE_TIMEOUT_SECONDS` | HTTP timeout | 15 seconds |

Salesforce settings are separate from database settings, so storage commands
work without them. Both sets are redacted in errors and representations.

## Phase 2: support chat

Three processes: the read MCP server (8091), the API with its run worker
(8081) and the Next.js app (3000), which proxies `/api/*` to the API. Add two
settings to `backend/.env.nabd` first:

```bash
python3 -c "import secrets; print('NABD_READ_MCP_CONTEXT_SECRET=' + secrets.token_urlsafe(48))" >> backend/.env.nabd
# and, with your own key (paid): NABD_OPENAI_API_KEY=...
```

With Docker (from `backend/`; rebuilds the image and migrates to 0002):

```bash
docker compose -f docker-compose.nabd.yml up -d --build --wait api
```

Or locally, in three terminals from `backend/` and `frontend/`:

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd migrate
PYTHONPATH=code .venv-text/bin/python -m nabd read-mcp serve
.venv-text/bin/python -m uvicorn nabd.app:app --app-dir code --port 8081
npm run dev
```

Open http://localhost:3000, pick a demo customer and chat. Without the two
settings the API still serves `/healthz` and `/readyz`, and the chat routes
answer 503 `chat_not_configured`.

| Setting | Meaning | Default |
| --- | --- | --- |
| `NABD_READ_MCP_CONTEXT_SECRET` | Shared API/MCP secret that signs each run's customer context (32+ chars) | None |
| `NABD_READ_MCP_URL` | MCP endpoint the API calls | `http://127.0.0.1:8091/mcp` |
| `NABD_READ_MCP_HOST` / `_PORT` / `_ALLOWED_HOSTS` | Server bind address and accepted `Host` headers | 127.0.0.1 / 8091 / local names |
| `NABD_OPENAI_API_KEY` | Key for both agents (paid); passed explicitly | None |
| `NABD_SUPERVISOR_MODEL`, `NABD_ORDERS_MODEL` | Models (Responses API) | `gpt-5.4-mini` |
| `NABD_CHAT_ALLOWED_ORIGINS` | Origins allowed to send state-changing requests | localhost:3000 |
| `NABD_CHAT_TURN_DEADLINE_SECONDS`, `_AGENT_TOOL_CALLS`, `_TURN_TOOL_CALLS` | Turn limits | 120, 5, 20 |
| `NABD_CHAT_RUN_LEASE_SECONDS`, `_MAX_RUN_ATTEMPTS`, `_WORKER_CONCURRENCY` | Run queue | 30, 3, 4 |
| `NABD_CHAT_SESSION_HOURS`, `_COOKIE_SECURE`, `_DEMO_SIGN_IN` | Demo session cookie | 12, false, true |
| `NABD_BUSINESS_TIMEZONE` | ShopEasy's calendar for "today" and return windows | Europe/London |
| `NABD_API_ORIGIN` (frontend) | Where Next.js proxies `/api/*` | `http://127.0.0.1:8081` |

Read MCP commands (from `backend/`, no model calls):

| Command | What it does |
| --- | --- |
| `python -m nabd read-mcp serve` | Serve Streamable HTTP on `NABD_READ_MCP_HOST:PORT` |
| `python -m nabd read-mcp smoke [--url URL]` | 12 read checks through the real server and Salesforce |
| `python -m nabd read-mcp prompt --customer C011` | Render the `staff_review` prompt for one customer |

## Phase 3: complaint cases

Every customer message now passes the complaint stage before the reply: the
Complaint specialist (a third model) reads the customer's cases and orders,
code decides the case action, and the **write MCP server** (8092) sends it to
the Salesforce case command gateway. The API needs one more secret, different
from the read one:

```bash
python3 -c "import secrets; print('NABD_WRITE_MCP_CONTEXT_SECRET=' + secrets.token_urlsafe(48))" >> backend/.env.nabd
```

Docker (from `backend/`) builds `nabd-backend:phase3`, migrates to 0003 and
starts `read-mcp`, `write-mcp` and `api`:

```bash
docker compose -f docker-compose.nabd.yml up -d --build --wait api
```

Locally, add a fourth terminal: `PYTHONPATH=code .venv-text/bin/python -m nabd write-mcp serve`.

| Setting | Meaning | Default |
| --- | --- | --- |
| `NABD_WRITE_MCP_CONTEXT_SECRET` | API/write-MCP secret for case-operation contexts (32+ chars, not the read secret) | None |
| `NABD_WRITE_MCP_URL` | Write MCP endpoint the API calls | `http://127.0.0.1:8092/mcp` |
| `NABD_WRITE_MCP_HOST` / `_PORT` / `_ALLOWED_HOSTS` | Server bind address and accepted `Host` headers | 127.0.0.1 / 8092 / local names |
| `NABD_COMPLAINTS_MODEL`, `NABD_COMPLAINTS_REASONING_EFFORT` | Complaint specialist model | `gpt-5.4-mini`, `low` |
| `NABD_CHAT_HISTORY_MESSAGES` | Conversation messages the specialist sees | 12 |
| `NABD_CHAT_REVIEW_CONFIDENCE` | Below this category confidence: General Support with review flag | 0.6 |
| `NABD_CHAT_RECONCILE_AFTER_SECONDS`, `_RECONCILE_INTERVAL_SECONDS`, `_MAX_RECONCILE_ATTEMPTS` | Background settling of unanswered case commands | 30, 10, 5 |

Salesforce changes are deployed with the usual `generate`, `deploy`,
`configure` (assigns `NABD_Integration_Gateway`), `seed` and `verify`
commands. Case write checks (from `backend/`, no model calls):

| Command | What it does |
| --- | --- |
| `python -m nabd write-mcp serve` | Serve Streamable HTTP on `NABD_WRITE_MCP_HOST:PORT` |
| `python -m nabd write-mcp smoke` | 15 checks through the real gateway; writes one labelled test case for C008 and closes it; reruns replay the same receipts |

Paid scenarios (ask the owner first; each runs once):

```bash
cd backend
.venv-text/bin/python -m pytest -m llm tests/nabd/test_case_scenarios.py -s   # 8 Phase 3 flows
.venv-text/bin/python -m pytest -m llm tests/nabd/test_chat_scenarios.py -s   # 12 Phase 2 flows as full turns
```

They use real models, real Salesforce reads and the in-memory gateway for
cases, so they are repeatable and leave no cases in the org.

Browser check without model costs: `.venv-text/bin/python scripts/browser_stack.py`
from `backend/` (real API, both MCP servers, Salesforce and the dev database on
8083/8093/8094 with scripted models), then
`NABD_API_ORIGIN=http://127.0.0.1:8083 npm run dev` in `frontend/`. Its demo
story writes a real test case for C006; close it in Salesforce afterwards.

## Verification

```bash
cd backend
# Free tests; PostgreSQL tests skip without the explicit test URL.
.venv-text/bin/python -m pytest -q

# Complete local suite, including real PostgreSQL and process-restart recovery:
docker compose -f docker-compose.nabd.yml --profile test up -d --wait test-db
NABD_TEST_DATABASE_URL=postgresql://nabd:nabd_test@127.0.0.1:55433/nabd_test \
  .venv-text/bin/python -m pytest -q

# Run the same suite entirely in the speech-free Linux image:
docker compose -f docker-compose.nabd.yml --profile test run --build --rm tests
```

Database tests create unique databases on the separate test service and remove
only those databases afterward. The opt-in URL must end in `_test`, and its role
must have database-creation permission (the provided test service does).
They never reset the application database. The test service uses disposable
storage; the application service uses its named volume.

The restart test runs two separate backend processes, checkpoints a real graph
containing a LangChain message, then restores the message and resumes the graph
with a new process. The Phase 2 chat tests serve the API with uvicorn against
real PostgreSQL, the in-memory read MCP server and scripted models, including a
server restart in the middle of a run. The Phase 3 tests add the in-memory
write MCP server and a fake gateway that follows the Apex rules: complaint
flows, crash and timeout recovery, the PostgreSQL advisory lock and background
reconciliation. No LLM or Salesforce calls occur in these checks.

Paid NABD scenarios (real models, real MCP and Salesforce; ask first):

```bash
.venv-text/bin/python -m pytest -m llm tests/nabd/test_chat_scenarios.py -s
```

## Dependencies and migrations

`requirements-text.in` declares runtime dependencies. `requirements-text.txt`
pins the complete resolved runtime for Python 3.14. The equivalent `-dev` files
add test dependencies while sharing the runtime pins. To refresh, resolve the
runtime input in a clean venv, freeze it, install the dev input constrained by
that runtime lock, and record only additional dev pins. Reverify on macOS and
the Linux container before accepting a lock refresh.

Application migrations live under `backend/migrations/` and use Alembic.
Revision `0001_nabd_schema` creates the `nabd` namespace. LangGraph owns its
checkpoint tables and migration sequence within that namespace. The
`python -m nabd migrate` command serializes both systems with a PostgreSQL
advisory lock. Forward upgrades are the normal path; the initial downgrade
deliberately refuses to remove a non-empty checkpoint schema.

Revision `0002_chat_runtime` adds demo sessions, conversations, messages, runs
and events (Phase 2). Operation and issue tables arrive with their consumers
in Phase 3.
