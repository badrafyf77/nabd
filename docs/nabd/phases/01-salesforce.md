# Phase 1 — Salesforce business system

**Status: complete (18 November 2026).** Depends on completed
[Phase 0](00-foundation.md), checkpoint `512e03b`. The handoff at the end of
this page records what was delivered and verified; the schema/operations
reference is [NABD Salesforce](../salesforce.md).

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

## Handoff — completed 18 November 2026

Git checkpoint: `1a04f5257292a6cfe16cc67c93207985638acdd6` (*Establish NABD Salesforce business system
(Phase 1)*) on `codex/nabd-chat-salesforce`.

### Delivered

- **Dedicated org** `nabd-dev` (Developer Edition, org ID `00DgL00000iV5JpUAK`,
  API 68.0), connected to the `sf` CLI (v2.158.7). No credentials are stored in
  the repository.
- **Schema contract** `backend/code/nabd/salesforce/schema.py` and generator
  `metadata.py` producing the SFDX project in `salesforce/force-app`
  (164 generated files: 9 custom objects incl. a custom metadata type, custom
  fields on Account/Contact/Case/Task, validation rules, layouts, tabs, the NABD
  Support app, five Case queues, two groups, two permission sets, Case
  status/origin value sets, a knowledge sharing rule and category routes).
  `python -m nabd salesforce check` and a test fail on any drift.
- **Hand-written source**: Apex `KnowledgeEntryRevisions` + trigger + tests
  (one published revision per entry), and the `NABD_Backend` External Client
  App with the client credentials flow. Its run-as policy lives in
  `salesforce/integration-app` and receives the username from the environment.
- **Access**: `NABD_Staff` for staff; API-only integration user with the
  Salesforce Integration licence, Salesforce API Integration permission set
  licence and read-only `NABD_Integration_Read`; drafts and retired knowledge
  hidden by sharing; internal case fields, evidence, risk and receipt objects
  not readable by the integration user.
- **Fixtures and seed**: seed date 18 November 2026; 16 customers, 35 orders,
  39 items, 35 deliveries, 24 knowledge revisions (22 published, 1 retired,
  1 draft), 7 multilingual cases, 10 evidence records, 3 tasks, 3 risk rules.
  Fingerprinted upserts preserve staff edits; `reset` is separate and needs
  `--confirm`.
- **Backend adapter**: `SalesforceSettings`, client-credentials token handling
  with one refresh on expiry, typed errors without secrets or fallback data,
  validated/escaped SOQL, and the customer-scoped typed read adapter.

### Verification results

| Check | Result |
| --- | --- |
| Validation-only deploy, then real deploy with `RunLocalTests` | Succeeded; Apex 4/4 passing, 100% coverage |
| `verify` against the org | **37/37**: schema/type/picklist/external-ID match for 13 objects, statuses, origin, queues, routes, fixture counts, order/item/delivery links, one published revision per entry, publication states, case queues and evidence, task links, staff/integration access, client app approval |
| First seed / repeated seed | 188 creates / **0 writes** (all unchanged, no duplicates) |
| Staff edit then seed | Edited case reported as preserved and left unchanged |
| Reset with wrong `--confirm` / correct `--confirm` | Refused (exit 2) / restored the one edited case; following seed wrote 0 |
| `smoke` as the integration user | **12/12**: auth, customer profile, customer-scoped orders, items/delivery, other customer's order invisible, case summaries, current published knowledge only, platform hides drafts/retired, internal field refused, writes refused, expired session refreshed, wrong secret fails without leaking |
| Offline suite, macOS, real PostgreSQL | **186 passed**, 45 paid scenario runs deselected |
| Offline suite, Linux test image | **178 passed, 8 skipped** (tests needing `salesforce/`, outside the image's build context), 45 deselected |
| Secret scan of changed files, `pip check`, `git diff --check` | No secrets or personal emails; no broken requirements; clean |

Offline checks use fakes and do not satisfy the org gate on their own; the
org gate is the deploy, verify, seed and smoke results above. No LLM calls
were made in Phase 1.

### Findings worth keeping

- Current `sf` CLI versions redact the token in `org display`; the developer
  commands read it through `sf org auth show-access-token` and never print it.
- Address parts are permissioned through compound fields
  (`Account.BillingAddress`); activity fields through `Task.`/`Event.`
  (`Activity.` entries were dropped on redeploy).
- The client app uses admin-approved users. Deploying its OAuth policy clears
  the approved permission sets, so `configure` grants
  `NABD_Integration_Read` access (a `SetupEntityAccess` row) after deploying it.
  Without that, tokens fail with `invalid_app_access`.
- The integration user's refused writes report
  `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` ("entity type cannot be updated").

### Known limitations

- Task page layouts are unchanged; the two NABD task fields are visible through
  the permission set but not placed on the Task layout.
- Seeded cases have the seed run as `CreatedDate` (backdating needs an extra
  audit permission); historical times live on evidence `Captured_At__c`.
- Fixture dates are fixed to the seed date. Re-seeding with a later
  `--seed-date` moves unedited records forward; staff-edited ones are preserved.
- Darija and Arabic fixture wording still needs the manual native review
  planned for Phase 4.
- Staff inspection was verified through metadata, layouts, permissions and
  data checks, not a recorded click-through of the Salesforce UI.
- Found after the checkpoint while checking the UI: custom object tabs
  (ShopEasy Orders, Knowledge Entries, risk and receipt objects) have no "All"
  list view, so they open on an empty "Recently Viewed" list. The records are
  reachable from accounts, cases and global search; see
  [viewing the data](../salesforce.md#viewing-the-data-in-salesforce).

### Resume commands

From `backend/` (the CLI must be logged in: `sf org login web --alias nabd-dev`):

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce check
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce deploy --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce configure --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce seed --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce verify --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce smoke
```

`smoke` needs `NABD_SALESFORCE_DOMAIN_URL`, `NABD_SALESFORCE_CLIENT_ID` and
`NABD_SALESFORCE_CLIENT_SECRET` in `backend/.env.nabd`. A new org also needs
`configure --create-integration-user` once.

Next entry criteria for Phase 2: these commands pass, and the read adapter is
the starting point for the authenticated read MCP. Phase 2 has not started.
