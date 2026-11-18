# Working on this repository

The active product is **NABD**, ShopEasy's multilingual text support and complaint
intelligence platform backed by Salesforce. The voice prototype is deferred.

## Start here

For NABD work, read [the new-chat entry point](docs/nabd/START_HERE.md), then the
current [status](docs/nabd/status.md) and the requested phase's plan. The complete
sequence is in [the roadmap](docs/nabd/roadmap.md).

- [Accepted decisions](docs/nabd/decisions.md) preserve the user's answers. Carry
  them forward instead of repeating the product interview.
- [Architecture](docs/nabd/architecture.md) and [contracts](docs/nabd/contracts.md)
  describe the target; status identifies what actually exists.
- Phases 0 (`512e03b`) and 1 (Salesforce business system, `1a04f52`) are
  complete. Phase 2 (support chat, `02178fa`) is implemented; its real-model English
  run passed 10/12, with two behaviour fixes open; Phase 3 follows. Later phase completions
  must update status and this pointer.
- Implement only the phase the user requests. Update its plan/status/handoff,
  verify it, and create a local checkpoint before declaring it complete.
- `docs/goals/`, `docs/plans/phase-1-*`, `backend/code/server.py`, and the original
  voice Docker files describe the legacy project. The old "Phase 1" is different
  from NABD Phase 1. Prefer `docs/nabd/` for active scope and acceptance criteria.

## Engineering expectations

- Use real model-driven tool calling and structured outputs. Code enforces
  authorization, contracts, business rules, and lifecycle transitions; do not
  interpret multilingual intent with regex/keyword routing.
- Salesforce owns business records **and FAQs/policies**. PostgreSQL owns
  conversation/execution state. Repository seed data is a fixture, not a live
  fallback business database.
- Follow the planned MCP read/write boundaries, customer scoping, durable jobs,
  idempotent operations, evidence-grounded decisions, and real execution events.
  Introduce these with their consumers in the relevant phase.
- The NABD agents are in `backend/code/nabd/agents/`. The legacy
  `backend/code/agents/` Supervisor/Orders graphs are voice-era code kept for
  their tests; do not describe them as NABD features.
- Run relevant tests for each change. Use `backend/.venv-text` and the pinned
  text dependencies; the complete Phase 0 verification included real PostgreSQL.
  Do not repeat unchanged full suites for documentation-only edits.
- Keep secrets out of notes, logs, fixtures, commits, and model-visible context.
  NABD loads `backend/.env.nabd`; it does not load the old `backend/.env`.
- Salesforce metadata is generated from `backend/code/nabd/salesforce/schema.py`;
  edit the contract and regenerate rather than hand-editing `salesforce/force-app`.

Commands and environment details are in [the development guide](docs/nabd/README.md).
