# Continue NABD in a new chat

This file is the durable entry point for a fresh engineer or agent. Everything
needed to resume is in the repository; the previous chat is not required.

## Current checkpoint

- Branch: `codex/nabd-chat-salesforce`.
- Completed: **NABD Phase 0** (`512e03b`) and **NABD Phase 1 — Salesforce
  business system** (`1a04f52`, full hash in [status](status.md)).
- **NABD Phase 2 — support chat and real-time agent progress** is implemented
  and verified without paid model calls. Its last gate item, a real-model run
  of 12 English scenarios, waits for the user's approval and OpenAI key.
- Last measured suite: **213 passed** (57 paid scenarios deselected) on macOS
  with real PostgreSQL. Real-org `salesforce verify` 39/39, `salesforce smoke`
  12/12, `read-mcp smoke` 12/12. See [status](status.md) for limits.
- The root `AGENTS.md` directs new sessions here. Later phase completions must
  update this checkpoint, status, roadmap, and that short pointer together.

## Read in this order

1. [Status and exact resume commands](status.md): actual implementation, verified
   results, current phase, environment snapshot, and next entry criteria.
2. [Accepted decisions](decisions.md): settled product choices and engineering
   requirements. Do not re-ask questions already answered here.
3. [Phase 2 handoff](phases/02-support-chat.md#handoff--18-november-2026): the
   read MCP server, agents, durable runs, events, API and chat, with what is
   verified and what is not. The [Salesforce reference](salesforce.md) covers
   the org and adapter underneath. Phase 3's scope is in the
   [roadmap](roadmap.md#phase-3--complaint-intelligence-and-reliable-cases);
   write its detailed plan first when Phase 3 is requested.
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
  read MCP server, chat API and Next.js chat exist (Phase 2); the chat answers
  503 `chat_not_configured` until `NABD_READ_MCP_CONTEXT_SECRET` and
  `NABD_OPENAI_API_KEY` are set. Write MCP, case writes and complaint analysis
  do not exist yet (Phase 3).
- `salesforce/force-app` metadata is generated from
  `backend/code/nabd/salesforce/schema.py`. Change the schema, then run
  `python -m nabd salesforce generate`; never hand-edit generated XML.
- Seed fixtures are deployment/test inputs. Runtime code reads Salesforce,
  never fixtures or the legacy SQLite shop database.
- `backend/code/server.py` and `docs/plans/phase-1-status.md` are legacy voice
  artifacts. Their Phase 1 is not NABD's next Phase 1.
- The legacy 113 unit tests still pass. The 45 legacy and 12 NABD paid
  scenario runs are deselected by default and were not executed in Phases 0–2.
- NABD agents live in `backend/code/nabd/agents/`; the legacy
  `agents/support` and `agents/orders` graphs are voice-era code kept for
  their tests, not the NABD runtime.
- Product commitments are fixed unless the user changes them. Tunable technical
  defaults in architecture need implementation/evaluation, not another broad
  product interview. Record evidence if an implementation default must change.

## Prompt for the next chat

> Continue NABD in this repository. Read AGENTS.md and docs/nabd/START_HERE.md.
> Phase 2 is implemented; its paid English scenario run is the remaining gate
> item (see docs/nabd/status.md). Ask me before any paid model run. When Phase 3
> is requested, write its plan first, start Phase 3 only, preserve all accepted
> decisions, and update the phase handoff and verification results when done.

For a later phase, replace the phase number and follow the then-current status.
