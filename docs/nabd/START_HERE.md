# Continue NABD in a new chat

This file is the durable entry point for a fresh engineer or agent. Everything
needed to resume is in the repository; the previous chat is not required.

## Current checkpoint

- Branch: `codex/nabd-phase7-quality-release`. Implemented at `6864092`;
  release verification is in progress. Phases 0–6 are merged into `main`.
- Completed: **NABD Phase 0** (`512e03b`) and **NABD Phase 1 — Salesforce
  business system** (`1a04f52`, full hash in [status](status.md)).
- Completed: **NABD Phase 2 — support chat and real-time agent progress**
  (`02178fa` plus handoff commits). Real-model English run: 10/12, then the two
  failures fixed and passing on a recheck.
- Completed: **NABD Phase 3 — complaint intelligence and reliable cases**
  (`9879593` plus the handoff commit `10c6c88`). Real-model run, once each:
  the 8 new case flows 7/8 and the 12 Phase 2 flows 9/12; after two prompt
  fixes 3 of those 4 failures passed a recheck, and `several_orders_needs_info`
  still fails its expectation (it lists returnable items instead of asking).
- Completed: **NABD Phase 4 — multilingual behavior** (`b4d5f71`). Approved
  real-model runs: 21/36, rechecks of the failures down to 1/1, then the final
  version **36/36** in one full run; manual review found wording slips only.
  See the [Phase 4 handoff](phases/04-multilingual.md#completion-summary).
- Completed: **NABD Phase 5 — live staff follow-up** (`575712a`),
  resumed after Phase 6 and rebased onto it. Durable Pub/Sub synchronization,
  public replies/withdrawal, labeled translations, SSE/snapshot/UI and recovery.
  Real staff loop 12/12, ten-change p95 2.696 s; see its
  [completion handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026).
- Completed: **NABD Phase 6 — analytics and proactive warnings** (`45a9445`
  plus the handoff commit): Salesforce risk engine, warnings with one task
  each, 13 reports and two dashboards. See the
  [Phase 6 handoff](phases/06-analytics-warnings.md#handoff--19-november-2026).
- Current: **Phase 7 — quality review and demonstration release**.
  The [Phase 7 handoff](phases/07-quality-release.md#implementation-handoff--19-november-2026)
  records the evaluator, distinct summary revision, free measurements and
  campaign status.
- Phase 5 baseline: **307 passed** (106 paid scenarios deselected) on macOS
  with real PostgreSQL. Phase 5: Apex 44/44, Salesforce verify 53/53, staff loop
  12/12; Salesforce/read-MCP/write-MCP smoke 12/12, 14/14, 15/15. The five
  approved translation examples passed across the initial run and a focused
  harness-error recheck. See [status](status.md) for limits and Linux results.
- The root `AGENTS.md` directs new sessions here. Later phase completions must
  update this checkpoint, status, roadmap, and that short pointer together.

## Read in this order

1. [Status and exact resume commands](status.md): actual implementation, verified
   results, current phase, environment snapshot, and next entry criteria.
2. [Accepted decisions](decisions.md): settled product choices and engineering
   requirements. Do not re-ask questions already answered here.
3. [Phase 5 handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026):
   live staff synchronization, translations, privacy/recovery and measured limits.
   [Phase 6 handoff](phases/06-analytics-warnings.md#handoff--19-november-2026):
   the Salesforce risk engine, dashboards, reconciliation and the
   integration-user lessons. [Phase 4 handoff](phases/04-multilingual.md#handoff):
   language/script selection, multilingual prompts, localized RTL activity, the
   Latin Darija script repair and every measured run, with its limitations. The
   [Phase 3 handoff](phases/03-complaints.md#handoff--18-november-2026) covers
   the complaint stage, case gateway and write MCP server, the
   [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) the
   chat underneath, and the [Salesforce reference](salesforce.md) the org.
4. The next phase's scope and gate in the [roadmap](roadmap.md) and the
   entry notes in [status](status.md#next-step). Phase 7's plan and handoff
   are in [07-quality-release.md](phases/07-quality-release.md).
5. [Architecture](architecture.md) and [interface contracts](contracts.md): target
   responsibilities, data ownership, agent flow, and future public interfaces.
6. [Roadmap](roadmap.md) and [development commands](README.md): later sequence,
   local setup, test opt-in, migration commands, and operating instructions.

## Inspect before changing anything

From the repository root:

```bash
git status --short --branch
git fetch origin && git log -1 --oneline origin/main
git log -5 --oneline
git show --stat 575712a
sf org display --target-org nabd-dev
```

Use the resume commands in status to recheck service health. Runtime state in
notes is an observation, not a guarantee that Docker, containers or the `sf`
CLI session are still available in the next session. If later commits exist,
inspect and preserve them; do not reset to the checkpoint or overwrite other
work. A session that opens on `main` or in a fresh worktree has every finished
phase; switch to (or recreate from `main`) the working branch before editing.

## Avoid these context mistakes

- The active product is NABD text support/complaint analysis. Voice work is
  deferred. ShopEasy remains the first demo business.
- Salesforce knowledge ownership is settled: **FAQs and policies live in
  Salesforce**, alongside customers, orders, deliveries, cases, and tasks.
- `/readyz` returning ready proves the storage foundation is ready. The
  chat answers 503 `chat_not_configured` until both MCP context secrets
  (`NABD_READ_MCP_CONTEXT_SECRET`, `NABD_WRITE_MCP_CONTEXT_SECRET`) and
  `NABD_OPENAI_API_KEY` are set. Complaint analysis and case writes exist
  (Phase 3); risk warnings exist in Salesforce only (Phase 6);
  staff-to-chat updates and labeled translations (Phase 5) run in the API.
- Deploy Salesforce only with `python -m nabd salesforce deploy`: it pauses
  the hourly risk job (Salesforce can refuse to redeploy Apex a scheduled job
  references) and schedules it again. `main` now includes Phase 5's
  Salesforce feed, so deploying `main` keeps the org whole.
- The risk engine runs in Salesforce. Work started by the integration user
  runs as that user: asynchronous Apex queued from the gateway could not
  create warnings, and a platform event it published without Create
  permission vanished silently. Case changes therefore publish
  `NABD_Risk_Check__e`, the integration user's only create right, and the
  event trigger evaluates as the Automated Process user. Apex tests do not
  reproduce licence limits; check new async paths with `risk-smoke`.
- `salesforce/force-app` metadata is generated from
  `backend/code/nabd/salesforce/schema.py`. Change the schema, then run
  `python -m nabd salesforce generate`; never hand-edit generated XML.
- Seed fixtures are deployment/test inputs. Runtime code reads Salesforce,
  never fixtures or the legacy SQLite shop database.
- `backend/code/server.py` and `docs/plans/phase-1-status.md` are legacy voice
  artifacts. Their Phase 1 is not NABD's next Phase 1.
- The legacy 113 unit tests still pass. The 106 paid scenarios (45 legacy,
  12 Phase 2, 8 Phase 3, 36 Phase 4, 5 staff translations) are deselected by default. Every NABD
  paid run so far was approved by the owner one run at a time; ask before any
  further paid run, and say which scenarios and how many times.
- NABD agents live in `backend/code/nabd/agents/`; the legacy
  `agents/support` and `agents/orders` graphs are voice-era code kept for
  their tests, not the NABD runtime.
- Case writes go only through the write MCP server and the Apex gateway
  `NabdCaseCommands`. The integration user has no object create/edit rights
  (one licence is shared by both MCP servers); keep it that way, and change
  case rules in the gateway and `nabd/agents/cases.py` together.
- Apex SOQL in the gateway must state `WITH SYSTEM_MODE`: implicit-mode queries
  on objects the integration user cannot read failed at runtime although the
  Apex tests (run as admin) passed.
- Latin Darija replies pass a writing-system check: one focused rewrite, then
  code conversion of leftover Arabic letters to Arabizi. It checks script, not
  meaning; keep it when adding new customer-facing Supervisor text.
- Browser checks use free scripted models on the real stack:
  `backend/scripts/browser_stack.py` (see its docstring). It writes a real
  test case to the org; close it afterwards.
- Git: commit and push as `badrafyf77` with no AI attribution lines; merge a
  finished phase into `main` and push (the owner's standing instruction).
- Risk warnings, tasks, sentiment and the negative streak never leave
  Salesforce: the integration user cannot read them (`verify` checks), and
  no NABD prompt, event or snapshot contains them.
- Product commitments are fixed unless the user changes them. Tunable technical
  defaults in architecture need implementation/evaluation, not another broad
  product interview. Record evidence if an implementation default must change.

## Prompt for the next chat

> Continue NABD in this repository. Read AGENTS.md, START_HERE and status.
> Phases 0–6 are complete. Resume the Phase 7 release campaign and handoff
> on the existing branch. Check completed measurements and any prior
> approvals. Get separate approval for each remaining paid run, and merge
> into main and push both branches as badrafyf77 once the phase is finished.
