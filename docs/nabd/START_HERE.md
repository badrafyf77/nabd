# Continue NABD in a new chat

This file is the durable entry point for a fresh engineer or agent. Everything
needed to resume is in the repository; the previous chat is not required.

## Current checkpoint

- Branch: `codex/nabd-chat-salesforce`. It equals `main` on GitHub at the end
  of Phase 4; finished phases are merged into `main`.
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
  real-model runs: 21/36, then rechecks of the failures down to 1/1; every
  failing scenario passed after its fix, but there is no fresh full 36/36 run
  of the final version. See the [Phase 4 handoff](phases/04-multilingual.md#completion-summary).
- Next: **NABD Phase 5 — live staff follow-up** (not started).
- Last measured suite: **275 passed** (101 paid scenarios deselected) on macOS
  with real PostgreSQL. Real-org `salesforce verify` 41/41, Apex 17/17,
  `salesforce smoke` 12/12, `read-mcp smoke` 14/14, `write-mcp smoke` 15/15
  (measured during Phase 4). See [status](status.md) for limits.
- The root `AGENTS.md` directs new sessions here. Later phase completions must
  update this checkpoint, status, roadmap, and that short pointer together.

## Read in this order

1. [Status and exact resume commands](status.md): actual implementation, verified
   results, current phase, environment snapshot, and next entry criteria.
2. [Accepted decisions](decisions.md): settled product choices and engineering
   requirements. Do not re-ask questions already answered here.
3. [Phase 4 handoff](phases/04-multilingual.md#handoff): language/script
   selection, multilingual prompts, localized RTL activity, the Latin Darija
   script repair and every measured run, with its limitations. The
   [Phase 3 handoff](phases/03-complaints.md#handoff--18-november-2026) covers
   the complaint stage, case gateway and write MCP server, the
   [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026) the
   chat underneath, and the [Salesforce reference](salesforce.md) the org.
4. Phase 5's scope and gate in the
   [roadmap](roadmap.md#phase-5--live-staff-follow-up) and the entry criteria in
   [status](status.md#next-step). Write `docs/nabd/phases/05-staff-follow-up.md`
   first when Phase 5 is requested.
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
git show --stat b4d5f71
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
  (Phase 3); staff-to-chat updates (Phase 5) and risk warnings (Phase 6) do not.
- `salesforce/force-app` metadata is generated from
  `backend/code/nabd/salesforce/schema.py`. Change the schema, then run
  `python -m nabd salesforce generate`; never hand-edit generated XML.
- Seed fixtures are deployment/test inputs. Runtime code reads Salesforce,
  never fixtures or the legacy SQLite shop database.
- `backend/code/server.py` and `docs/plans/phase-1-status.md` are legacy voice
  artifacts. Their Phase 1 is not NABD's next Phase 1.
- The legacy 113 unit tests still pass. The 45 legacy and 20 NABD paid
  scenario runs are deselected by default. The NABD scenarios ran once each
  in Phases 2 and 3 with the user's approval; ask before any further paid run.
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
- Browser checks use free scripted models on the real stack:
  `backend/scripts/browser_stack.py` (see its docstring). It writes a real
  test case to the org; close it afterwards.
- Git: commit and push as `badrafyf77` with no AI attribution lines; merge a
  finished phase into `main` and push (the owner's standing instruction).
- Product commitments are fixed unless the user changes them. Tunable technical
  defaults in architecture need implementation/evaluation, not another broad
  product interview. Record evidence if an implementation default must change.

## Prompt for the next chat

> Continue NABD in this repository. Read AGENTS.md and docs/nabd/START_HERE.md,
> then docs/nabd/status.md. Phases 0–4 are complete. Start Phase 5 (live staff
> follow-up): first write docs/nabd/phases/05-staff-follow-up.md in the same form
> as the Phase 4 plan, then implement Phase 5 only. Preserve all accepted
> decisions, ask me before any paid model run, update the phase handoff and
> verification results, and when Phase 5 is finished merge it into main and
> push as badrafyf77.

For a later phase, replace the phase number and follow the then-current status.
