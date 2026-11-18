# Continue NABD in a new chat

This file is the durable entry point for a fresh engineer or agent. Everything
needed to resume is in the repository; the previous chat is not required.

## Current checkpoint

- Branch: `codex/nabd-chat-salesforce`.
- Completed: **NABD Phase 0** (`512e03b`) and **NABD Phase 1 — Salesforce
  business system** (`1a04f52`, full hash in [status](status.md)).
- Next: **NABD Phase 2 — support chat and real-time agent progress**, not started.
- Last measured suite: **186 passed, 45 deselected** on macOS with real
  PostgreSQL; Linux image 178 passed, 8 skipped. Real-org Salesforce `verify`
  37/37 and integration `smoke` 12/12. See [status](status.md) for limits.
- The root `AGENTS.md` directs new sessions here. Later phase completions must
  update this checkpoint, status, roadmap, and that short pointer together.

## Read in this order

1. [Status and exact resume commands](status.md): actual implementation, verified
   results, current phase, environment snapshot, and next entry criteria.
2. [Accepted decisions](decisions.md): settled product choices and engineering
   requirements. Do not re-ask questions already answered here.
3. [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026)
   and the [Salesforce reference](salesforce.md): the org, schema, access,
   fixtures, commands and read adapter Phase 2 builds on. Phase 2's scope is in
   the [roadmap](roadmap.md#phase-2--support-chat-and-real-time-agent-progress);
   write its detailed plan first when Phase 2 is requested.
4. [Architecture](architecture.md) and [interface contracts](contracts.md): target
   responsibilities, data ownership, agent flow, and future public interfaces.
5. [Roadmap](roadmap.md) and [development commands](README.md): later sequence,
   local setup, test opt-in, migration commands, and operating instructions.

## Inspect before changing anything

From the repository root:

```bash
git status --short --branch
git log -3 --oneline
git show --stat 512e03b
git show --stat 1a04f52
sf org display --target-org nabd-dev
```

Use the resume commands in status to recheck service health. Runtime state in
notes is an observation, not a guarantee that Docker, containers or the `sf`
CLI session are still available in the next session. If later commits exist, inspect and preserve them; do not
reset to the checkpoint or overwrite other work.

## Avoid these context mistakes

- The active product is NABD text support/complaint analysis. Voice work is
  deferred. ShopEasy remains the first demo business.
- Salesforce knowledge ownership is settled: **FAQs and policies live in
  Salesforce**, alongside customers, orders, deliveries, cases, and tasks.
- `/readyz` returning ready proves the storage foundation is ready. The
  Salesforce org, seed, integration user and read adapter exist (Phase 1).
  Chat endpoints, MCP servers, case writes and the NABD frontend do not.
- `salesforce/force-app` metadata is generated from
  `backend/code/nabd/salesforce/schema.py`. Change the schema, then run
  `python -m nabd salesforce generate`; never hand-edit generated XML.
- Seed fixtures are deployment/test inputs. Runtime code reads Salesforce,
  never fixtures or the legacy SQLite shop database.
- `backend/code/server.py` and `docs/plans/phase-1-status.md` are legacy voice
  artifacts. Their Phase 1 is not NABD's next Phase 1.
- The legacy 113 unit tests still pass; NABD adds 17 foundation and 56
  Salesforce cases. The 45 deselected paid scenario runs were not executed in
  Phases 0 or 1.
- Product commitments are fixed unless the user changes them. Tunable technical
  defaults in architecture need implementation/evaluation, not another broad
  product interview. Record evidence if an implementation default must change.

## Prompt for the next chat

> Continue NABD in this repository. Read AGENTS.md and docs/nabd/START_HERE.md.
> Phases 0 and 1 are complete. Write and follow a Phase 2 plan from the roadmap,
> building on docs/nabd/salesforce.md. Start Phase 2 only, preserve all accepted
> decisions, and update the phase handoff and verification results when finished.

For a later phase, replace the phase number and follow the then-current status.
