# Continue NABD in a new chat

This file is the durable entry point for a fresh engineer or agent. Everything
needed to resume is in the repository; the previous chat is not required.

## Current checkpoint

- Branch: `codex/nabd-chat-salesforce`.
- Completed: **NABD Phase 0**, commit `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b`.
- Next: **NABD Phase 1 — Salesforce business system**, not started.
- Last measured suite: **130 passed, 45 deselected**, on macOS and the CPU-only
  Linux image. See [status](status.md) for the actual checks and their limits.
- The root `AGENTS.md` directs new sessions here. Later phase completions must
  update this checkpoint, status, roadmap, and that short pointer together.

## Read in this order

1. [Status and exact resume commands](status.md): actual implementation, verified
   results, current phase, environment snapshot, and next entry criteria.
2. [Accepted decisions](decisions.md): settled product choices and engineering
   requirements. Do not re-ask questions already answered here.
3. [Phase 1 plan](phases/01-salesforce.md): next work, boundaries, setup needs,
   verification, and completion handoff.
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
```

Use the resume commands in status to recheck service health. Runtime state in
notes is an observation, not a guarantee that Docker or containers are running
in the next session. If later commits exist, inspect and preserve them; do not
reset to the checkpoint or overwrite other work.

## Avoid these context mistakes

- The active product is NABD text support/complaint analysis. Voice work is
  deferred. ShopEasy remains the first demo business.
- Salesforce knowledge ownership is settled: **FAQs and policies live in
  Salesforce**, alongside customers, orders, deliveries, cases, and tasks.
- `/readyz` returning ready proves the storage foundation is ready. Chat, MCP,
  Salesforce integration, and the NABD frontend have not been implemented yet.
- `backend/code/server.py` and `docs/plans/phase-1-status.md` are legacy voice
  artifacts. Their Phase 1 is not NABD's next Phase 1.
- The legacy 113 unit tests still pass; the new 17 cases cover foundation behavior.
  The 45 deselected paid scenario runs were not executed in Phase 0.
- Product commitments are fixed unless the user changes them. Tunable technical
  defaults in architecture need implementation/evaluation, not another broad
  product interview. Record evidence if an implementation default must change.

## Prompt for the next chat

> Continue NABD in this repository. Read AGENTS.md and docs/nabd/START_HERE.md,
> then follow docs/nabd/phases/01-salesforce.md. Phase 0 is complete at 512e03b.
> Start Phase 1 only, preserve all accepted decisions, and update the phase
> handoff and verification results when finished. Continue useful local work
> while handling any Salesforce signup or authorization step that needs me.

For a later phase, replace the phase number and follow the then-current status.
