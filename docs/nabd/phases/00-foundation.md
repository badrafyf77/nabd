# Phase 0 — Establish the NABD foundation

Approved on 18 November 2026. Implementation status and verification live in
[status](../status.md); operating commands live in the [development guide](../README.md).

## Objective

Prepare ShopEasy for independent text development and later Salesforce
integration. Preserve reusable agent behavior and record a reliable baseline.

## Work and acceptance

| Step | Deliverable | Acceptance |
| --- | --- | --- |
| Starting point | `codex/nabd-chat-salesforce`, baseline commit/tests, deferred issues | Starting state and reuse boundaries documented |
| Decisions and handoff | Roadmap, accepted decisions, architecture, status | Later session can resume without repeating product questions |
| Text application | Independent FastAPI startup/dependencies/settings | Imports/startup succeed without speech packages/GPU/model credentials |
| Durable storage | PostgreSQL pool, Alembic and checkpointer migrations | Explicit repeatable/concurrent migration works; health/readiness reflect state |
| Checkpointing | Real AsyncPostgresSaver shared through app lifespan | A real graph/message restores and resumes across processes |
| Reproducibility | CPU-only Compose, isolated Python dependencies, env template | macOS and Linux checks; fresh setup/run commands documented |
| Design contracts | Agent findings, complaints, commands, activity events | Minimum interfaces documented; implement concrete types with later consumers |

## Boundaries

Phase 0 ships health/readiness and storage foundation. Salesforce setup starts
in Phase 1. Customer chat/agent adaptation starts in Phase 2; complaint agent and
case mutations start in Phase 3. Existing voice behavior is a legacy baseline,
and unresolved voice issues do not block these phases.

Use the official LangGraph PostgreSQL checkpointer and Alembic rather than a
custom checkpoint or migration protocol. A migration service owns DDL; API
readiness prevents serving unmigrated storage. Keep dependency pins and runtime
settings distinct from the voice stack.

## Checks

- Existing free unit-test baseline before changes.
- Configuration validation/redaction and no speech import regressions.
- Health while DB is unreachable; readiness for missing/outdated schema.
- Real PostgreSQL migrations, repeated/concurrent runners, and recovery of a
  graph containing a LangChain message in a separate new backend process.
- CPU-only image build, container health, and API/DB recovery checks.

All checks are free of LLM and Salesforce calls. Document actual results, not
just planned checks. Checkpoint the verified work and leave subsequent phases
pending.
