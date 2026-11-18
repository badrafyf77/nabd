# NABD status

Updated: 18 November 2026 (Africa/Casablanca).

## Current work

**Phases 0–4 are complete; Phases 5–7 are pending.** Working branch:
`codex/nabd-chat-salesforce`, merged into `main` at the end of Phase 4.
Phase 4 measured its real-model results in six approved runs (21/36, then
rechecks of the failures down to 1/1); every failing scenario passed after
its fix, but no fresh full 36-scenario run of the final version exists. See
the [Phase 4 handoff](phases/04-multilingual.md#completion-summary).

| Phase | Checkpoint | Handoff |
| --- | --- | --- |
| 0 — Foundation | `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b` | [Phase 0 plan](phases/00-foundation.md) |
| 1 — Salesforce business system | `1a04f5257292a6cfe16cc67c93207985638acdd6` | [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026) |
| 2 — Support chat and live activity | `02178faa969f4747d58de98c0f1b1a043f9a9ad9` (implementation; docs and real-model results in later commits) | [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) |
| 3 — Complaint intelligence and reliable cases | `987959317beae7339f3bb51958032df0cb427b45` (implementation; handoff in the next commit) | [Phase 3 handoff](phases/03-complaints.md#handoff--18-november-2026) |
| 4 — Multilingual behavior | `b4d5f710f2ea5289bbda8762c8f078081d9cac61` (last implementation commit; earlier `e352b0a`, `94957e5`, `e7c876a`, `dbccbf1`, `f341970`) | [Phase 4 handoff](phases/04-multilingual.md#handoff) |

Completed phases are merged into `main` on GitHub
(`badrafyf77/customer-support-voice-agent`, private); Phases 0–3 were merged
on 18 November 2026, Phase 4 later the same day. Inspect a checkpoint with `git show --stat <commit>`.
Later documentation commits are handoff updates, not new implementation phases.

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

Phase 3 — complaints become reliable Salesforce cases during the chat:

- a mandatory **complaint stage** before every reply: the **Complaint
  specialist** (third model) reads the customer's case history and orders and
  returns a typed assessment; code verifies it and applies matching,
  lifecycle, routing/review and priority rules (refunds become Returns &
  Refunds staff cases);
- the **Apex case command gateway** `NabdCaseCommands` (receipts, request
  hashes, versions, locks, transitions, staff overrides, evidence
  de-duplication) and the **write MCP server** (`python -m nabd write-mcp`);
- **durable commands** in PostgreSQL (plan stored before sending, lookup
  before resend, one conflict re-read, per-customer advisory lock, background
  reconciliation) and issue links;
- live **case cards** and a confirmed-case strip in the chat; the Supervisor
  cites only confirmed case work.

Phase 4 implements language/script selection in the complaint assessment,
durable language events/snapshots, original evidence scripts, multilingual
agent prompts, localized chat/activity templates and RTL, plus a fresh
customer-scoped order index before classification and a Latin Darija script
repair (focused rewrite, then code Arabizi conversion). Staff-to-chat updates,
risk warnings and quality review do not exist yet.

## Verification

| Check | Actual result |
| --- | --- |
| Phase 1 baseline | 186 passed (macOS, real PostgreSQL); Linux image 178 passed, 8 skipped |
| Phase 2 baseline | 213 passed (macOS, real PostgreSQL); Linux image 205 passed, 8 skipped |
| Offline suite, macOS, real PostgreSQL (Phase 3) | **249 passed**; 65 paid scenarios deselected (45 legacy, 12 Phase 2, 8 Phase 3) |
| Offline suite, Linux test image (Phase 3) | **240 passed, 9 skipped** (need `salesforce/`, outside the image), 65 deselected |
| Salesforce deploy with local Apex tests | Succeeded; **Apex 17/17** |
| `salesforce verify` against the org | **41/41** |
| Seed after the Phase 3 fields, repeated seed | 7 cases updated, 1 case and 2 evidence created, none preserved; then 0 writes |
| `salesforce smoke` as the integration user | 12/12 (direct writes still refused) |
| `read-mcp smoke` through the real server and org | **12/12** |
| `write-mcp smoke` through the real gateway | **15/15**; a rerun replayed every receipt (1 smoke case, 7 receipts) |
| Browser check (scripted models; real API, both MCP servers, Salesforce, PostgreSQL) | Complaint → confirmed case 00001044 with live cards; follow-up updated the same case; reload restored cards and case strip |
| Docker Compose `nabd-backend:phase3` | migrate 0003; `read-mcp`, `write-mcp`, `api` healthy; ready |
| Frontend `tsc` | Clean |
| `pip check`, `git diff --check`, secret scan | Clean |
| Offline suite, macOS, real PostgreSQL (Phase 4 final) | **275 passed**; 101 paid scenarios deselected |
| Real-model multilingual scenarios (Phase 4, approved runs) | 21/36, recheck 13/19, focused 3/6, then 0/3, 2/3, 1/1; every failure passed after its fix; no fresh 36/36 run |
| Real-model English scenarios (20, `-m llm`, once each) | Phase 3 set **7/8**, Phase 2 set as full turns **9/12** (after two over-strict checks were fixed); after two prompt fixes 3 of the 4 failures **pass** on recheck; `several_orders_needs_info` still fails its expectation |

Details are in the [Phase 3 handoff](phases/03-complaints.md#verification-results),
the [Phase 2 handoff](phases/02-support-chat.md#verification-results) and the
[Phase 1 handoff](phases/01-salesforce.md#verification-results). Paid LLM
calls were made only in the approved scenario runs of Phases 2 and 3.

Open legacy voice issues remain documented in `docs/plans/phase-1-status.md`;
voice work is deferred and they do not block NABD phases.

## Last observed runtime — 18 November 2026

- Salesforce: `nabd-dev` connected to `sf` CLI 2.158.7; integration user active
  with `NABD_Integration_Read` and `NABD_Integration_Gateway`.
- `backend/.env.nabd` (Git-ignored) has database, Salesforce,
  `NABD_OPENAI_API_KEY`, `NABD_READ_MCP_CONTEXT_SECRET` and
  `NABD_WRITE_MCP_CONTEXT_SECRET` (both secrets generated locally in Phase 3;
  values never recorded). The chat is enabled.
- Docker: dev `db` (55432) and `test-db` (55433) running; the Phase 3 stack
  (`read-mcp` 8091, `write-mcp` 8092, `api` 8081, image
  `nabd-backend:phase3`) was started and healthy. The dev database is at
  migration 0003.
- Phase 4 observation: `nabd-backend:phase4` is healthy on the same ports with
  migration 0003. The standard registry-backed build timed out; cached Phase 3
  images plus current source verified the unchanged dependencies locally.
  The free scripted browser stack is stopped; the frontend on 3000 now points
  to the real API on 8081. The C006 browser case 00001044 was closed as withdrawn
  after the multilingual checks (directly verified in Salesforce).
- Test data in the org from Phase 3 checks: smoke case 00001043 (C008,
  closed), probe 00001042 (C008, closed as withdrawn) and browser case
  00001044 (C006, closed as withdrawn).
- GitHub: `origin` is `badrafyf77/customer-support-voice-agent` (private).
  Pushes use the GitHub CLI login:
  `git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push …`
  after checking `gh api user -q .login` prints `badrafyf77`.

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
PYTHONPATH=code .venv-text/bin/python -m nabd write-mcp smoke
```

To run the chat: `docker compose -f docker-compose.nabd.yml up -d --build --wait api`
and `npm run dev` in `frontend/` (http://localhost:3000). A fresh checkout
needs both MCP secrets (32+ random characters each) and `NABD_OPENAI_API_KEY`
in `backend/.env.nabd` first. If `sf org display` reports an expired session,
run `sf org login web --alias nabd-dev`.

## Next step

**Phase 5 — live staff follow-up.** Scope and gate are in the
[roadmap](roadmap.md#phase-5--live-staff-follow-up). Entry criteria: the
resume commands above pass, and the [Phase 4 limitations](phases/04-multilingual.md#current-limitations-to-carry-forward)
and [Phase 3 limitations](phases/03-complaints.md#known-limitations) are
carried forward. Write `docs/nabd/phases/05-staff-follow-up.md` first, in the
same form as the Phase 4 plan.

When Phase 5 is finished: update its handoff, this status, START_HERE, the
roadmap and the AGENTS pointer, commit as `badrafyf77`, then merge into `main`
and push both branches.

Model costs: the user pays for OpenAI. Use the free fake-model tests for most
checks, and ask the user before any paid `pytest -m llm` or other real-model run.
