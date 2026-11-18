# NABD status

Updated: 18 November 2026 (Africa/Casablanca).

## Current work

**Phases 0–3 are complete. Phase 4 is in progress; Phases 5–7 are pending.**
Phase 4's [plan and running handoff](phases/04-multilingual.md) record the
implementation, free checks and approved paid evaluation. Working branch:
`codex/nabd-chat-salesforce`, started from the Phase 3 handoff `603545d`.
Phase 4 checkpoints: `e352b0a`, `94957e5`, `e7c876a`, `dbccbf1`, latest `f341970`.
The first paid run measured 21/36 automatic passes; its 19-scenario recheck
passed 13/19. The focused confirmation of the final six passed 3/6: two
over-strict checks (now fixed) and one genuine Latin Darija script failure,
fixed by a focused rewrite plus a code Arabizi fallback. The recheck of those
three passed 0/3 on new prompt-behavior faults (generic clarification, asking
to confirm a single matching order); the script held. Prompt fixes pass 275
free tests; the second recheck passed 2/3 (both Darija returns fixed;
English unspecified return still generic, now fixed in the complaint prompt,
unmeasured). `main` still holds Phase 3.
Continue Phase 4 on that branch and merge it into `main` when Phase 4 is
finished.

| Phase | Checkpoint | Handoff |
| --- | --- | --- |
| 0 — Foundation | `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b` | [Phase 0 plan](phases/00-foundation.md) |
| 1 — Salesforce business system | `1a04f5257292a6cfe16cc67c93207985638acdd6` | [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026) |
| 2 — Support chat and live activity | `02178faa969f4747d58de98c0f1b1a043f9a9ad9` (implementation; docs and real-model results in later commits) | [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) |
| 3 — Complaint intelligence and reliable cases | `987959317beae7339f3bb51958032df0cb427b45` (implementation; handoff in the next commit) | [Phase 3 handoff](phases/03-complaints.md#handoff--18-november-2026) |

Completed phases are merged into `main` on GitHub
(`badrafyf77/customer-support-voice-agent`, private); Phases 0–3 were merged
on 18 November 2026. Inspect a checkpoint with `git show --stat <commit>`.
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
agent prompts, localized chat/activity templates and RTL. Free checks pass;
real-model quality evaluation is in progress. Staff-to-chat updates, risk
warnings and quality review do not exist yet.

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

**Phase 4 — multilingual behavior** is in progress. Scope and
gate are in the [roadmap](roadmap.md#phase-4--multilingual-behavior); the
language rules are in [decisions](decisions.md#product-and-data). Entry
criteria: the resume commands above pass.

The [Phase 4 plan](phases/04-multilingual.md) was written first; implementation
and free checks are complete, with the approved real-model evaluation running.

### Starting points for Phase 4 (facts about the code, not decisions)

- **Prompts are English-only**: `nabd/agents/prompts/supervisor.md` says
  "English"; `orders.md` and `complaints.md` assume English input. The
  Complaint specialist already returns a `language` per issue
  (`en`/`fr`/`ar`/`ary`/`mixed`), stored as `Customer_Language__c` and on
  evidence as `Original_Language__c`; `Original_Script__c` is not set yet.
- **Knowledge** has `Language__c` with only `en`; the read MCP returns
  English revisions. Policy outcomes come from code (`return_check`).
- **Activity texts** are English templates keyed by stable codes in
  `frontend/src/chat/messages.ts`; bubbles use `dir="auto"`, cards do not set
  direction.
- **Fixtures** already hold Darija (both scripts), Arabic, French and mixed
  customers C011–C016 with cases and original wording; FX-ISSUE-0002 (C011)
  waits on the customer.
- **Case matching** compares new messages with English case summaries;
  cross-language matching is the model's job, not code's.
- **Tests to reuse**: scripted flows in `tests/nabd/test_complaints.py`; the
  paid harness `tests/nabd/live_turns.py` supports multi-turn conversations
  (real Salesforce reads, fixture cases); `backend/scripts/browser_stack.py`
  runs the real stack with scripted models for browser checks.
- **Phase 3 caveats carried forward**: see the
  [Phase 3 known limitations](phases/03-complaints.md#known-limitations).

When Phase 4 is finished: update its handoff, this status, START_HERE, the
roadmap and the AGENTS pointer, commit as `badrafyf77`, then merge into `main`
and push both branches.

Model costs: the user pays for OpenAI. Use the free fake-model tests for most
checks, and ask the user before any paid `pytest -m llm` or other real-model run.
