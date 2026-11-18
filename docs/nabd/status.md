# NABD status

Updated: 18 November 2026 (Africa/Casablanca).

## Current work

**Phases 0 and 1 are complete and verified. Phases 2–7 are pending.**
Branch: `codex/nabd-chat-salesforce`.

| Phase | Checkpoint | Handoff |
| --- | --- | --- |
| 0 — Foundation | `512e03bb2f4bec5c00aeb851cfbf7f3d09942c0b` | [Phase 0 plan](phases/00-foundation.md) |
| 1 — Salesforce business system | `1a04f5257292a6cfe16cc67c93207985638acdd6` | [Phase 1 handoff](phases/01-salesforce.md#handoff--completed-18-november-2026) |

Inspect a checkpoint with `git show --stat <commit>`. Later documentation
commits are handoff updates, not new implementation phases.

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
[NABD Salesforce](salesforce.md). Chat endpoints, MCP servers, case
mutations and the NABD frontend do not exist yet.

## Verification

| Check | Actual result |
| --- | --- |
| Phase 0 baseline (before Phase 1) | 130 passed, 45 deselected (macOS and Linux) |
| Offline suite, macOS, real PostgreSQL | **186 passed**, 45 paid scenario runs deselected |
| Offline suite, Linux test image | **178 passed, 8 skipped**, 45 deselected; skips need `salesforce/`, outside the image context |
| Salesforce deploy with local Apex tests | Succeeded; Apex 4/4, 100% coverage |
| `verify` against the org | 37/37 checks passed |
| Seed, repeated seed | 188 creates; then 0 writes |
| Staff-edit preservation and explicit reset | Edited record preserved; reset refused without matching confirmation; reset restored it |
| `smoke` as the integration user | 12/12 checks passed |
| `pip check`, `git diff --check`, secret scan | Clean |

Details and the full check list are in the
[Phase 1 handoff](phases/01-salesforce.md#verification-results). No paid LLM
calls were made in Phase 0 or Phase 1. The 45 deselected runs are the legacy
English LLM scenarios (15 × 3), not a new measured model score.

Open legacy voice issues remain documented in `docs/plans/phase-1-status.md`;
voice work is deferred and they do not block NABD phases.

## Last observed runtime — 18 November 2026

- Salesforce: `nabd-dev` connected to `sf` CLI 2.158.7; integration user
  `nabd-integration.00dgl00000iv5jpuak@example.com` active.
- `backend/.env.nabd` exists locally (Git-ignored) with database and
  Salesforce integration settings. Its secret values are not recorded anywhere.
- Disposable PostgreSQL test service (localhost:55433) was running for the
  suite. API/DB containers from Phase 0 were not rechecked in this session.

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
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce smoke
```

If `sf org display` reports an expired session, run
`sf org login web --alias nabd-dev`. The Phase 0 API stack still starts with
`docker compose -f docker-compose.nabd.yml up -d --build --wait api`.

## Next phase

Phase 2 — support chat and real-time agent progress — is next and has not
started. Entry criteria: the commands above pass. Its read MCP should wrap
`nabd.salesforce.reads.SalesforceReads`; extend `NABD_Integration_Read` only
together with the features that need more fields. Start Phase 2 only when the
user requests it.
