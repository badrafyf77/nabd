# NABD development guide

NABD is ShopEasy's multilingual text support and complaint-intelligence product.
Salesforce owns business records and knowledge; PostgreSQL owns execution state.
The implementation progresses through phases 0–7. **For a new chat, start with
[START_HERE](START_HERE.md)** for the checkpoint, reading order, pitfalls, and
copyable continuation prompt. Current progress is in [status](status.md). Phase 1
delivered the [Salesforce business system](salesforce.md); Phase 2 is next in the
[roadmap](roadmap.md).

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
with a new process. No LLM or Salesforce calls occur in these checks.

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

Conversation, event-log, and operation tables are added with their first real
consumers in later phases. Phase 0 does not invent unused business tables.
