# NABD status

Updated 19 November 2026 (Africa/Casablanca) after checking the Phase 7 handoff and local services.

## Current work

**Phases 0–6 are complete. Phase 7 is implemented and locally checkpointed at
`2fabd9f` on `codex/nabd-phase7-quality-release`.** Local `main` and
`origin/main` are at `0079407`. The current Phase 7 branch is local, with
release gates and merge pending.
[Phase 7 handoff](phases/07-quality-release.md#implementation-handoff--19-november-2026).

| Phase | Checkpoint | Handoff |
| --- | --- | --- |
| 0 — Foundation | `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b` | [Phase 0 plan](phases/00-foundation.md) |
| 1 — Salesforce business system | `1a04f5257292a6cfe16cc67c93207985638acdd6` | [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026) |
| 2 — Support chat and live activity | `02178faa969f4747d58de98c0f1b1a043f9a9ad9` (implementation; docs and real-model results in later commits) | [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) |
| 3 — Complaint intelligence and reliable cases | `987959317beae7339f3bb51958032df0cb427b45` (implementation; handoff in the next commit) | [Phase 3 handoff](phases/03-complaints.md#handoff--18-november-2026) |
| 4 — Multilingual behavior | `b4d5f710f2ea5289bbda8762c8f078081d9cac61` (last implementation commit; earlier `e352b0a`, `94957e5`, `e7c876a`, `dbccbf1`, `f341970`) | [Phase 4 handoff](phases/04-multilingual.md#handoff) |
| 5 — Live staff follow-up | `575712a` (completed after Phase 6, rebased onto it) | [Phase 5 completion handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026) |
| 6 — Analytics and proactive warnings | `45a9445` (implementation; handoff in the next commit) | [Phase 6 handoff](phases/06-analytics-warnings.md#handoff--19-november-2026) |
| 7 — Quality review and demonstration release | `6864092` implementation, `9d2a352` paid recheck, `2fabd9f` final harness and verification | [Phase 7 handoff](phases/07-quality-release.md#implementation-handoff--19-november-2026) |

Completed phases are merged into `main` on GitHub
(`badrafyf77/customer-support-voice-agent`, private); Phases 0–3 were merged
on 18 November 2026, Phase 4 later the same day, Phases 6 then 5 on 19 November. Inspect a checkpoint with `git show --stat <commit>`.
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
repair (focused rewrite, then code Arabizi conversion).

Phase 6 — analytics and proactive warnings, all inside Salesforce:

- an Apex **risk engine** for three versioned rules (repeat complaints, High
  priority unresolved for 24 hours, negative sentiment at the next follow-up),
  run after case changes (an after-commit risk check event, evaluated as the
  Automated Process user) and hourly; one warning per customer and rule with
  its evidence and one follow-up task; clearing with history, recurrence;
- the case gateway records each customer contact's sentiment and a
  negative-contact streak; formula fields give open-since and age band;
- 13 generated reports and two dynamic **dashboards** (NABD Complaints, NABD
  Risk Warnings) with data freshness, reconciled against SOQL;
- commands `analytics-reconcile` and `risk-smoke`; `deploy` pauses the
  hourly job, `configure` schedules it.

Customers and the backend see none of the internal risk data. Phase 5 adds
public staff follow-up. Phase 7 adds private quality review.

Phase 5 — live staff follow-up:

- an advisory-locked Pub/Sub subscriber with durable replay cursors and a
  transactional case-sync inbox, generation counters and fenced leases;
- current authorized case/comment projection, own-command echo suppression,
  browser/reconnect/periodic reconciliation and privacy withdrawals;
- public staff replies and status/action notices on existing SSE/snapshots,
  original plus labeled translation, five template sets/RTL and staff context
  for later agent turns (never accepted as customer evidence);
- migration `0004_staff_sync`, API lifespan wiring and `staff-sync smoke`.
  See the [Phase 5 handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026).

Phase 7 — migration `0005_quality`, bounded operator traces and token
capture, a tool-free evaluator, fenced retries, and an idempotent
summary-only Salesforce gateway operation. Operator quality and risk
reports, release campaign harness and reproducible [demo](demo.md) are
available. [Operations runbook](runbook.md).

## Verification

Phase 7 free verification measured 326 macOS and 313 Linux passes with
PostgreSQL (13 Linux skips), 43 Apex passes and 53 Salesforce verification
checks. Event-to-browser p95 was 34 ms across 405 events. Staff status
changes reached the browser within five seconds in 19 of 20 isolated
samples. The full results are in [Phase 7 evidence](evidence/phase7-free.json).
The initial paid run returned 52/60. The owner's focused recheck passed
3/8. A harness bug affected one scenario in both attempts and was fixed
with a regression check across three pytest modules. The full gate and
labeled evaluator run remain pending. [Paid evidence](evidence/phase7-campaign.json).

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
| Real-model multilingual scenarios (Phase 4, approved runs) | 21/36, recheck 13/19, focused 3/6, then 0/3, 2/3, 1/1; final version **36/36** in one full run (manual review: wording slips only) |
| Offline suite, macOS, real PostgreSQL (Phase 6) | **293 passed**; 101 paid scenarios deselected |
| Salesforce deploy with local Apex tests (Phase 6) | Succeeded; **Apex 41/41** |
| `salesforce verify` / `analytics-reconcile` / `risk-smoke` (Phase 6) | **51/51** / **15/15** / **17/17** (twice) |
| `salesforce smoke` / `read-mcp smoke` / `write-mcp smoke` after Phase 6 | **12/12** / **14/14** / **15/15** |
| Real-model English scenarios (20, `-m llm`, once each) | Phase 3 set **7/8**, Phase 2 set as full turns **9/12** (after two over-strict checks were fixed); after two prompt fixes 3 of the 4 failures **pass** on recheck; `several_orders_needs_info` still fails its expectation |
| Offline suite, macOS, real PostgreSQL (Phase 5) | **307 passed**, 106 paid scenarios deselected |
| Offline suite, Linux test image (Phase 5) | **294 passed, 13 skipped**, 106 deselected (Salesforce metadata outside image) |
| Salesforce Apex / verify (Phase 5) | **44/44** / **53/53** |
| Real staff-sync smoke | **12/12**, n=10 status changes, **p95 2.696 s**, max 2.696 s to durable customer event |
| Salesforce / read-MCP / write-MCP smoke after Phase 5 | **12/12** / **14/14** / **15/15** |
| Approved staff translation checks | First run 3 passed, 2 harness event-loop errors; focused approved recheck **2/2**, all five examples completed |
| Browser, scripted models on the real stack (Phase 5) | Live staff reply/status/action, labeled French translation, reload without duplicate, private note absent, live withdrawal |

Details are in the [Phase 5 handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026),
the [Phase 6 handoff](phases/06-analytics-warnings.md#verification-results),
the [Phase 4 handoff](phases/04-multilingual.md#handoff),
the [Phase 3 handoff](phases/03-complaints.md#verification-results),
the [Phase 2 handoff](phases/02-support-chat.md#verification-results) and the
[Phase 1 handoff](phases/01-salesforce.md#verification-results).
Each paid scenario run requires separate owner approval.

Open legacy voice issues remain documented in `docs/plans/phase-1-status.md`;
voice work is deferred and they do not block NABD phases.

## Last observed runtime — 19 November 2026

- Compose `api` (8081), `read-mcp` (8091), and `write-mcp` (8092) are
  healthy on `nabd-backend:phase7`. `/readyz` returned `ready`. PostgreSQL
  dev (55432) is at Alembic revision `0005_quality`. Test DB (55433) is healthy.
- The standard frontend on 3000 and the scripted frontend on 3001 were
  stopped when checked. See [demo](demo.md) to start either walkthrough.
- The `nabd-dev` Salesforce CLI session returned API limits. Phase 7's
  wording-only gateway and restricted receipt value were deployed and previously
  verified. The Phase 7 browser case 00001054 was closed as a duplicate and its
  temporary comment was removed. [Deployment and cleanup evidence](phases/07-quality-release.md#implementation-handoff--19-november-2026).
- `backend/.env.nabd` is present and Git-ignored. Keep Salesforce, OpenAI
  and MCP credentials local. See [setup](README.md#fresh-checkout) for a new
  environment.
- `origin/main` was checked at `0079407`. The private GitHub repository is
  `badrafyf77/customer-support-voice-agent`. For a future authorized push,
  check `gh api user -q .login` for `badrafyf77` and use the GitHub CLI
  credential helper as needed.

Recheck services and credentials when starting a new session.

## Exact resume commands

From the repository root:

```bash
git switch codex/nabd-phase7-quality-release
git status --short --branch
git log -4 --oneline
cd backend
docker compose -f docker-compose.nabd.yml ps -a
curl --fail http://127.0.0.1:8081/readyz
sf org list limits --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd quality report --since 2026-11-19
```

For startup, migrations, and optional verification, follow the
[development guide](README.md#fresh-checkout) and [operator runbook](runbook.md).
Use the [scripted demo](demo.md#free-scripted-walkthrough) for a walkthrough
without paid model calls.

## Next step

Resume only if the owner requests more Phase 7 release work. The authorized
paid work ended with the eight-scenario recheck. Any future campaign uses
the repaired runner on a frozen revision and requires explicit approval for
each 60-scenario run and the separate 12-call labeled evaluator check.
The strict staff update target also remains open after one of 20 isolated
updates exceeded five seconds. See the [roadmap gates](roadmap.md#phase-7--quality-review-and-demonstration-release)
and [follow-up backlog](backlog.md). Once release verification is complete,
update the handoff, merge into `main`, and push both branches as
`badrafyf77` after scanning the push range for secrets.
