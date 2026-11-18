# NABD status

Updated: 18 November 2026 (Africa/Casablanca).

## Current work

**Phases 0–2 are complete. Phases 3–7 are pending.** Branch: `codex/nabd-chat-salesforce`.

| Phase | Checkpoint | Handoff |
| --- | --- | --- |
| 0 — Foundation | `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b` | [Phase 0 plan](phases/00-foundation.md) |
| 1 — Salesforce business system | `1a04f5257292a6cfe16cc67c93207985638acdd6` | [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026) |
| 2 — Support chat and live activity | `02178faa969f4747d58de98c0f1b1a043f9a9ad9` (implementation; docs and real-model results in later commits) | [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) |

Completed phases are merged into `main` on GitHub
(`badrafyf77/customer-support-voice-agent`, private); Phases 0–2 were merged
on 18 November 2026. Inspect a checkpoint with `git show --stat <commit>`. Later documentation
commits are handoff updates, not new implementation phases.

## Delivered so far

Phase 0 — independent `nabd.app:app` with `/healthz` and `/readyz`; validated,
redacted settings; pooled PostgreSQL; explicit serialized Alembic/checkpointer
migrations; lifespan-owned persistent checkpointer; pinned Python 3.14
speech-free dependencies; CPU-only image and Compose stack.

Phase 1 — a dedicated Salesforce Developer Edition org (`nabd-dev`, org ID
`00DgL00000iV5JpUAK`, API 68.0) holding the ShopEasy business model, staff
knowledge, queues and access model, deployed from the repository:

- one schema contract (`backend/code/nabd/salesforce/schema.py`) that
  generates the SFDX metadata in `salesforce/`, maps the seed and whitelists
  the backend's queries;
- repeatable `deploy`, `configure`, `seed`, `reset`, `verify` and `smoke`
  commands (`python -m nabd salesforce …`);
- seeded fixtures (seed date 18 November 2026): legacy 10 customers/25 orders
  unchanged plus six Moroccan/French customers, multilingual complaint cases
  with original evidence, versioned FAQ/policy revisions and risk rules;
- an API-only integration user behind the `NABD_Backend` External Client App
  (client credentials flow), read-only and customer-safe;
- the typed, customer-scoped Salesforce read adapter with token refresh and
  secret-free errors.

The reference for objects, fields, access and commands is
[NABD Salesforce](salesforce.md).

Phase 2 — English text support chat for signed-in demo customers:

- a customer-scoped **read MCP server** (`python -m nabd read-mcp`) over the
  Phase 1 adapter, authenticated by per-run signed customer context;
- async **Supervisor** and **Orders** agents using real MCP calls, typed and
  evidence-checked findings, tool budgets and a turn deadline;
- PostgreSQL **sessions, conversations, messages, a durable run queue and an
  event log**, with checkpoint resume after a crash;
- the **HTTP/SSE API** and the **Next.js chat** with live activity cards,
  replay and reconnect. Salesforce gained `All` list views and the
  `Policy_Days__c` return-window value.

Case writes, complaint analysis and other languages do not exist yet.

## Verification

| Check | Actual result |
| --- | --- |
| Phase 1 baseline | 186 passed (macOS, real PostgreSQL); Linux image 178 passed, 8 skipped |
| Offline suite, macOS, real PostgreSQL (Phase 2) | **213 passed**; 57 paid scenarios deselected (45 legacy, 12 NABD English) |
| Offline suite, Linux test image (Phase 2) | **205 passed, 8 skipped** (need `salesforce/`, outside the image), 57 deselected |
| Salesforce deploy with local Apex tests | Succeeded; Apex 4/4 |
| `salesforce verify` against the org | **39/39** |
| Seed after adding `Policy_Days__c`, repeated seed | 24 knowledge revisions refreshed, none preserved; then 0 writes |
| `salesforce smoke` as the integration user | 12/12 |
| `read-mcp smoke` through the real server and org | **12/12** |
| Browser check (scripted models; real API, MCP, Salesforce, PostgreSQL) | Sign-in, streaming, nested cards, queueing, reload restore, API kill → reconnect → resumed run |
| Frontend `tsc`, `next build` | Clean |
| `pip check`, `git diff --check`, secret scan | Clean |
| Real-model English scenarios (12, `-m llm`, once each) | **10/12 pass**; the 2 failures (a wrong delivery month, a missing refund statement) were fixed and **pass** on one recheck |

Details are in the [Phase 2 handoff](phases/02-support-chat.md#verification-results)
and the [Phase 1 handoff](phases/01-salesforce.md#verification-results). No
paid LLM calls were made in Phases 0–1. Phase 2's only paid calls were the
approved single English scenario run.

Open legacy voice issues remain documented in `docs/plans/phase-1-status.md`;
voice work is deferred and they do not block NABD phases.

## Last observed runtime — 18 November 2026

- Salesforce: `nabd-dev` connected to `sf` CLI 2.158.7; integration user active.
- `backend/.env.nabd` (Git-ignored) has database, Salesforce and
  `NABD_OPENAI_API_KEY` settings (values never recorded). It does not yet have
  `NABD_READ_MCP_CONTEXT_SECRET`, so the browser chat stays disabled (503
  `chat_not_configured`) until it is added.
- Docker: dev `db` (55432), `test-db` (55433) and the **Phase 0** API image
  on 8081 were running. That container and the dev database were left at
  migration 0001; Phase 2 needs `up -d --build` (below), which migrates to 0002.

Recheck these in every new session; a recorded healthy state does not imply
the CLI session, Docker or containers are still available.

## Exact resume commands

From the repository root:

```bash
git switch codex/nabd-chat-salesforce
cd backend
docker compose -f docker-compose.nabd.yml --profile test up -d --wait test-db
NABD_TEST_DATABASE_URL=postgresql://nabd:nabd_test@127.0.0.1:55433/nabd_test \
  .venv-text/bin/python -m pytest -q
sf org display --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce verify --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd read-mcp smoke
```

To run the chat: add `NABD_READ_MCP_CONTEXT_SECRET` (32+ random characters)
and `NABD_OPENAI_API_KEY` to `backend/.env.nabd`, then
`docker compose -f docker-compose.nabd.yml up -d --build --wait api` and
`npm run dev` in `frontend/` (http://localhost:3000). If `sf org display`
reports an expired session, run `sf org login web --alias nabd-dev`.

## Next step

Phase 3 — complaint intelligence and reliable cases — is next and has not
started. Scope is in the
[roadmap](roadmap.md#phase-3--complaint-intelligence-and-reliable-cases); write
its plan first, in the same form as the Phase 2 plan. Phase 2's real-model
evidence is single runs (see the
[handoff](phases/02-support-chat.md#real-model-english-run--18-november-2026)).

Model costs: the user pays for OpenAI. Use the free fake-model tests for most
checks, and ask the user before any paid `pytest -m llm` or other real-model run.
