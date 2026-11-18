# Phase 1 — Salesforce business system

**Status: pending.** Depends on completed [Phase 0](00-foundation.md), checkpoint
`512e03b`. This plan records the approved next phase; writing it does not start
Salesforce setup or deployment.

## Outcome

A dedicated Salesforce development org contains repeatably deployed ShopEasy
business records and staff-maintained knowledge. Staff can inspect them in
Salesforce, and the NABD backend can authenticate and perform scoped read smoke
checks against the real org.

Read [accepted decisions](../decisions.md) first. The [roadmap](../roadmap.md),
[architecture](../architecture.md), and [contracts](../contracts.md) provide the
target context; [status](../status.md) identifies actual delivered behavior.

## Starting facts and external setup

- No Salesforce CLI connection, org, metadata, credentials, or integration code
  was created/verified during Phase 0. Existing account availability is unknown;
  inspect local tooling and configured aliases before asking about it.
- A dedicated development org is the user's chosen environment. Signup/email
  confirmation or interactive authorization may require the user. Ask only for
  the specific missing action, without requesting secret values in chat.
- Continue local schema/metadata, fixtures, integration code, and tests while
  waiting for such a setup step. Real-org validation remains required before
  claiming Phase 1 complete.
- Configure new credentials explicitly; do not copy or expose the legacy voice
  `.env`. Document required names in the NABD example environment as their
  consumers are implemented.

## Implementation sequence

1. Inspect Phase 0 status, branch, dependencies, migration/readiness behavior,
   available Salesforce CLI/tooling, and any existing org aliases. Recheck the
   relevant foundation before changing it.
2. Establish a repository-managed Salesforce metadata project. Define concrete
   fields/relationships, stable external identifiers, permission sets, and
   staff layouts for the agreed model below. Keep those definitions in one
   schema contract used by deployment, seed mapping, and integration code.
3. Configure the dedicated org, External Client App, client-credentials flow,
   and restricted integration permissions. Add server-side token handling and
   a narrow typed read adapter with validated query parameters and redaction.
   Prefer current official Salesforce documentation over legacy Connected App
   tutorials; record required org capabilities and actual API version.
4. Add repeatable metadata deployment and synthetic-data upsert commands. Use
   stable ShopEasy identifiers; preserve existing cases/data and staff edits on
   re-run. Make fixture resets separate and explicit, never the default seed.
5. Preserve existing 10-customer/25-order scenarios from `backend/code/shop/`,
   then add Moroccan and French customers and representative multilingual
   complaint fixtures. Record the seed date so relative dates are reproducible.
   Keep shared policies and GBP. Fixtures remain inputs, not live SQLite fallback.
6. Configure department queues, category mappings, case statuses, and staff
   record access. Seed published English FAQ/policy revisions with source IDs;
   original-language complaint evidence remains unmodified.
7. Verify deployment, seed repetition, authentication, record relationships,
   scoped reads, and knowledge publication against the real development org.
   Save results and exact next commands.

## Agreed data model

| Data | Representation | Required behavior |
| --- | --- | --- |
| Customers | Account/Contact with stable ShopEasy IDs | Customer/order/case ownership is explicit |
| Orders, items, deliveries | Custom ShopEasy objects | Preserve existing domain relationships/status/date/price data |
| Complaints | Standard Case plus NABD analysis/lifecycle fields | Link customer, order/issue, summary, evidence, language, category and outcome |
| Follow-up | Standard Task | Link to the relevant customer/case and assigned staff |
| FAQs/policies | Versioned `KnowledgeEntry__c` records | Staff-editable draft/published/retired revisions, retrievable published content |
| Original evidence | Case-linked records with stable source-message IDs | Preserve original customer text and distinguish reports from verified facts |
| Risk warnings | Rule/episode/evidence/task linkage | Support later deduplicated warnings; no risk engine in Phase 1 |
| Operation receipts | Unique operation identity and result linkage | Support the transactional write gateway in Phase 3 |

Finalize exact field names and the Account/Contact representation in the Phase 1
schema artifact after inspecting org capabilities. Match consumer semantics;
do not let setup-specific choices change the accepted product scope. Use
custom knowledge records as planned, so optional Salesforce Knowledge licensing
does not become a dependency or move knowledge into the repository.

Queues: Delivery; Orders & Products; Billing & Payments; Returns & Refunds;
General Support. Statuses: New; Working; Waiting on Customer; Waiting on Staff;
Closed. These prepare later workflows; Phase 1 does not execute agent decisions.

## Verification and completion gate

- Metadata deploys to the dedicated org and matches the documented schema.
- Staff can inspect customers, orders/items/deliveries, knowledge, and queue
  configuration. Record links and publication state are correct.
- Upserts retain stable identifiers and re-running seed creates no duplicates
  or unintended overwrites of staff-modified records.
- The backend authenticates with server-side credentials and retrieves only
  the requested customer's representative data; unrelated records and draft
  knowledge are excluded by adapter contracts.
- Token-expiration/authentication failures are handled without leaking secrets
  or falling back to fabricated/local business data.
- Offline adapter/fixture checks, real-org smoke results, and relevant Phase 0
  regressions are recorded separately. Mocks alone do not satisfy the org gate.

## Phase boundary and handoff

MCP servers and customer chat are Phase 2. Complaint analysis, live case
mutations, and the transactional Apex command gateway are Phase 3. Full language
behavior is Phase 4, live staff synchronization Phase 5, risk/dashboard behavior
Phase 6, and the background evaluator Phase 7. Do not implement these early just
because their data structures exist in Phase 1.

Before completion, update status, this plan, the roadmap, START_HERE, and the root
AGENTS pointer. Record org alias/ID without credentials, deployed metadata,
seed date/counts, test evidence, configuration names, known limitations, the Git
checkpoint, and exact deploy/seed/read-smoke resume commands. Start no later
phase automatically.
