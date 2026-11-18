# NABD status

Updated: 18 November 2026 (Africa/Casablanca).

## Current work

**Phase 0 is complete and verified. Phases 1–7 are pending.**
Branch: `codex/nabd-chat-salesforce`.

Implementation checkpoint: **`512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b`**
(`Establish NABD text foundation (Phase 0)`). Later documentation commits are
handoff updates, not new implementation phases. Inspect the actual checkpoint:

```bash
git show --stat 512e03b
```

Starting point: commit `379eac4` (`Write down where Phase 1 stopped`) on `phase-1`.
Working tree was clean. Baseline verification before changes:
`backend/.venv/bin/python -m pytest -q` from `backend`:
**113 passed, 45 deselected in 2.54 seconds**.

## Baseline interpretation

The 113 free tests cover reusable graph/tool budgets, Supervisor/Orders behavior,
customer-scoped ShopEasy reads, scenario assertions, tracing, configuration, and
the legacy phone/name identity adapter. The 45 deselected runs are fifteen
English LLM scenarios repeated three times, not a new measured model score.

Open legacy issues are documented in `docs/plans/phase-1-status.md`: full voice
call completion, paused spoken-phone parsing, voice call ending after transfer,
full paid-scenario rerun after the last prompt change, and old Docker/Modal
verification. Voice work is deferred. Agent facts/ownership/scenarios remain
reuse candidates; voice timing, phone identity, direct SQLite integration, and
English-only spoken prompts require adaptation for NABD.

## Delivered foundation

- Independent `nabd.app:app` with `/healthz` and `/readyz`.
- Dedicated validated/redacted settings and `.env.nabd.example`.
- Pooled PostgreSQL, explicit serialized Alembic/checkpointer migrations, strict
  checkpoint deserialization, and lifespan-owned persistent checkpointer.
- Pinned Python 3.14 runtime/dev dependency sets, isolated `.venv-text`, CPU-only
  image, named database volume, migration service, and isolated test DB/service.
- Accepted decisions, target architecture/contracts, eight-phase roadmap, and
  Phase 0 plan/handoff documentation.

## Verification

| Check | Actual result |
| --- | --- |
| Original baseline before changes | 113 passed; 45 paid scenario runs deselected |
| Full suite, clean text venv on macOS/Python 3.14.4 | **130 passed, 45 deselected**, 11.23s |
| Full suite, speech-free Linux test image | **130 passed, 45 deselected**, 10.88s; no warnings |
| New Phase 0 checks | 17 passing cases, including real PostgreSQL |
| Explicit migration and concurrent runners | Repeated upgrades pass; concurrent runners serialize |
| Missing DB/schema/checkpoint tables | Liveness 200; readiness 503; no secret/error details in response |
| Separate backend process restart | Original LangChain message restores; graph resumes count 1 -> 2 |
| Running API container restart | Checkpoint restores and resumes against the named-volume database |
| Running PostgreSQL outage/restart | Liveness stays 200, readiness becomes 503 then recovers to 200; graph state survives |
| Runtime image isolation | Non-root UID 10001; no torch, torchaudio, STT, TTS, or faster-whisper packages |
| Dependency compatibility | `pip check`: no broken requirements |
| Docs and patch formatting | Local document links resolve; `git diff --check` passes |

The concurrency test exposed an interaction between a blocking advisory-lock
statement and LangGraph's concurrent index creation. Migration acquisition now
uses short `pg_try_advisory_lock` attempts with a 60-second acquisition deadline;
the test enforces bounded completion. Keep this regression coverage.

No paid LLM calls or Salesforce requests were used. These results validate the
foundation, not the future multilingual agent behavior or Salesforce integration.

## Last observed runtime — 18 November 2026

- API: `http://localhost:8081`; `/healthz` and `/readyz` both return 200.
- PostgreSQL: localhost:55432; named volume `nabd_nabd_postgres`.
- One-shot migration service exited successfully. Disposable test DB is stopped.
- Backend code in the container matches the verified Phase 0 implementation.
- Local Python environment: `backend/.venv-text` (ignored by Git).

Recheck these services in every new session. A recorded healthy endpoint does
not imply Docker is running later. The fixture credentials in the development
guide are local-only examples; do not save real integration secrets in notes.

## Exact resume commands

From the repository root:

```bash
git switch codex/nabd-chat-salesforce
cd backend
docker compose -f docker-compose.nabd.yml up -d --build --wait api
curl --fail http://localhost:8081/readyz
docker compose -f docker-compose.nabd.yml --profile test up -d --wait test-db
NABD_TEST_DATABASE_URL=postgresql://nabd:nabd_test@127.0.0.1:55433/nabd_test \
  .venv-text/bin/python -m pytest -q
```

For a fresh checkout, first install the pinned text-dev dependencies as described
in the development guide, or run the all-container test command there.

## Resume and next phase

Begin with [START_HERE](START_HERE.md), then the detailed
[Phase 1 plan](phases/01-salesforce.md). It links the remaining accepted decisions,
architecture, development commands, and roadmap in the intended reading order.

Phase 1 entry criteria: Phase 0 verification passes and a dedicated Salesforce
development org can be registered/configured. Org creation and credentials are
Phase 1 setup work, not an unfinished Phase 0 implementation.

No Salesforce connection, tooling, org configuration, or credentials were
created/verified during Phase 0. Inspect current local capabilities before
asking for any necessary signup/authorization action.

The next authorized phase should implement the Salesforce org/model/knowledge
setup, repeatable metadata/seed deployment, and authenticated read smoke checks.
No chat endpoints, MCP server, Salesforce schema, or frontend redesign have been
implemented early. Start Phase 1 only when the user requests it.
