# NABD Salesforce business system

Phase 1 reference: what lives in Salesforce, how it is deployed and seeded,
and how the backend reads it. The executable contract is
[`backend/code/nabd/salesforce/schema.py`](../../backend/code/nabd/salesforce/schema.py);
this page explains it. If the two ever disagree, the code is right and this
page needs fixing.

## Org

| Item | Value |
| --- | --- |
| Org | Dedicated Developer Edition, CLI alias `nabd-dev` |
| Org ID | `00DgL00000iV5JpUAK` |
| My Domain | `https://orgfarm-78cf9356bb-dev-ed.develop.my.salesforce.com` |
| API version | 68.0, the org's current version and the SFDX `sourceApiVersion` |
| Integration user | `nabd-integration.00dgl00000iv5jpuak@example.com` |
| Org settings left unchanged | Time zone America/Los_Angeles, locale en_US, default currency (amounts use GBP number fields instead) |

Capabilities this design relies on, checked in the org on 18 November 2026:
one free **Salesforce Integration** user licence, the **Salesforce API
Integration** permission set licence, the *Minimum Access - API Only
Integrations* profile, and External Client App metadata. Person accounts are
not enabled (they need Salesforce support), so each customer is a business
Account plus one Contact.

## Data model

| Data | Salesforce object | Stable key (upsert/external ID) | Notes |
| --- | --- | --- | --- |
| Customer | `Account` + `Contact` | `ShopEasy_Customer_Id__c` (C001) on both | One Account per customer; `Preferred_Language__c` on Account |
| Order | `ShopEasy_Order__c` | `Order_Number__c` (SE-1042) | Required lookup to Account (restrict delete); GBP `Total_GBP__c` |
| Order line | `ShopEasy_Order_Item__c` | `Item_Key__c` (SE-1042-1) | Master-detail to order; `Returnable__c` |
| Delivery | `ShopEasy_Delivery__c` | `Delivery_Key__c` (order number) | Master-detail to order; current and original expected dates |
| Complaint | standard `Case` | `NABD_Issue_Id__c` | Category, language, sentiment, urgency, confidence, review flag, English summary, resolution and reopen fields |
| Original evidence | `Complaint_Evidence__c` | `Evidence_Key__c` (issue:message) | Master-detail to Case; exact original text; kind = customer report, verified fact or customer confirmation |
| Follow-up | standard `Task` | `NABD_Task_Key__c` (matched by query) | Linked by `WhatId` to a Case or Risk Episode; activities cannot hold unique external IDs |
| FAQ/policy | `KnowledgeEntry__c` | `Revision_Key__c` (key@revision) | Draft, Published, Retired revisions of one `Entry_Key__c` |
| Risk rule | `Risk_Rule__c` | `Rule_Key__c` (code@version) | Three starting rules seeded; no engine until Phase 6 |
| Risk warning | `Risk_Episode__c`, `Risk_Evidence__c` | `Episode_Key__c`, `Evidence_Key__c` | One episode per customer/rule; holds its single follow-up task ID |
| Operation receipt | `NABD_Operation_Receipt__c` | `Operation_Id__c` | Structure for the Phase 3 write gateway; empty in Phase 1 |

Case statuses are exactly **New, Working, Waiting on Customer, Waiting on
Staff, Closed** (the org's default `Escalated` was removed). `NABD Chat` was
added to Case Origin. Five queues own cases, one per complaint category, and
the custom metadata type `NABD_Category_Route__mdt` records the mapping:

| Category value | Queue |
| --- | --- |
| `Delivery` | `NABD_Delivery` (Delivery) |
| `Orders_Products` | `NABD_Orders_Products` (Orders & Products) |
| `Billing_Payments` | `NABD_Billing_Payments` (Billing & Payments) |
| `Returns_Refunds` | `NABD_Returns_Refunds` (Returns & Refunds) |
| `General_Support` | `NABD_General_Support` (General Support; also ambiguous cases) |

### Knowledge revisions

Staff edit FAQs and policies in Salesforce. The Apex trigger
`KnowledgeEntryTrigger` (class `KnowledgeEntryRevisions`, 100% test
coverage) sets `Revision_Key__c` from entry key and revision, stamps
publish/retire times, and retires the previously published revision whenever
a new one is published. Two revisions of one entry cannot be published in the
same save. Validation rules require body text before publishing and revision
numbers from 1. To change an answer, create revision N+1 as Draft, then set
it to Published.

## Access

| Who | Access |
| --- | --- |
| Staff (`NABD_Staff` permission set, `NABD_Staff` group) | NABD Support app and tabs; read/create/edit on NABD objects, view all custom records; receipts read-only; queue member of all five queues |
| Backend integration user (`NABD_Integration_Read`, `NABD_Integration` group) | API only; **read-only** on Account, Contact, Case, orders, items, deliveries and knowledge; only the customer-facing fields the read adapter returns |

`KnowledgeEntry__c` is private. A criteria sharing rule shares only
`Status__c = Published` revisions with the `NABD_Integration` group, so the
platform itself hides drafts and retired revisions from the backend. The
integration user cannot read internal case fields (sentiment, urgency,
confidence, review flag, English summary, description) or evidence, risk and
receipt objects. Later phases extend these permissions together with the
features that need them.

The backend authenticates through the **NABD_Backend External Client App**
with the OAuth 2.0 client credentials flow, running as the integration user.
Its client ID and secret live only in `backend/.env.nabd` (Git-ignored) as
`NABD_SALESFORCE_CLIENT_ID` and `NABD_SALESFORCE_CLIENT_SECRET`, next to
`NABD_SALESFORCE_DOMAIN_URL`. To get them: Setup → External Client App Manager
→ NABD Backend → Settings → OAuth Settings → Consumer Key and Secret.

## Fixtures

[`fixtures.py`](../../backend/code/nabd/salesforce/fixtures.py) is the only
fixture source. Seed date **18 November 2026**: every date is an offset from it,
so the same seed date always gives the same records.

| Set | Count | Notes |
| --- | --- | --- |
| Customers | 16 | Legacy C001–C010 unchanged (English); C011–C016 Moroccan/French: 3 Darija, 1 Arabic, 2 French. All live in UK cities because ShopEasy only delivers to the UK |
| Orders / items / deliveries | 35 / 39 / 35 | Legacy 25 orders unchanged; 10 new covering delay, damage, duplicate charge, loss and replacement, refund |
| Knowledge revisions | 24 | 14 FAQs + 8 policies published (legacy facts in written form, e.g. £3.99); `faq.delivery_cost@1` retired; `faq.support_hours@2` draft |
| Cases / evidence / tasks | 7 / 10 / 3 | English, French, Arabic, Darija (Latin and Arabic script) and a French/Darija mix; every status; one closed with customer confirmation; one ambiguous case flagged for review |
| Risk rules | 3 | Repeat complaints, unresolved High priority, persistent negative sentiment |

Fixtures are deployment and test inputs. The running product never reads
them or the legacy SQLite database as a fallback.

## Viewing the data in Salesforce

Log in at login.salesforce.com, open the App Launcher (nine dots) and choose
**NABD Support**.

| To see | Where |
| --- | --- |
| Customers | Accounts tab → **All Accounts** |
| A customer's orders | Open the account → **ShopEasy Orders** related list |
| Items and delivery | Open an order → its item and delivery lists |
| Complaints | Cases tab → **NABD Delivery**, **NABD Billing & Payments**, … or **NABD Needs Review**; evidence is on each case |
| Follow-up tasks | Open a case (for example FX-ISSUE-0001) → activity |
| FAQs and policies | Global search (for example "delivery cost") |

Known gap: custom objects deployed through metadata have no "All" list view,
so the ShopEasy Orders, Knowledge Entries, Risk Rules, Risk Episodes and
Operation Receipts tabs only offer the empty "Recently Viewed" list. Add
`All` list views to the generator (planned as the first small task of
Phase 2). Salesforce also auto-created a queue list view per queue
("Delivery", …) beside the generated "NABD Delivery", … views; the duplicates
are harmless.

## Commands

Run from `backend/` with `PYTHONPATH=code .venv-text/bin/python -m nabd salesforce …`:

| Command | What it does |
| --- | --- |
| `generate` / `check` | Write, or verify, `salesforce/force-app` metadata from the schema contract |
| `deploy --target-org nabd-dev` | Refuse stale metadata, deploy `force-app`, run local Apex tests |
| `configure --target-org nabd-dev [--create-integration-user]` | Staff permission set/group for the CLI user; integration user, licence, permission set, group; deploy the client app policy with the run-as username |
| `seed --target-org nabd-dev [--seed-date YYYY-MM-DD]` | Create missing fixtures; update unedited fixtures whose definition changed; never overwrite staff edits |
| `reset --target-org nabd-dev --confirm nabd-dev` | Explicitly restore fixture values over staff edits (deletes nothing) |
| `verify --target-org nabd-dev` | Compare live schema, picklists, queues, routes, fixture records, links and access with the contract |
| `smoke` | Read checks as the integration user, using only `backend/.env.nabd` |

The seed stores `NABD_Seed_Fingerprint__c`, a hash of the values it wrote. A
record whose current values no longer match that hash was edited after
seeding and is reported as preserved. Deployment and seeding use the
developer's `sf` CLI session; the backend never uses that session.

## Read adapter

[`reads.py`](../../backend/code/nabd/salesforce/reads.py) returns typed
results with a `RecordRef` (record ID and `SystemModstamp`) for evidence:
`get_customer`, `list_orders`, `get_order`, `list_cases`,
`list_published_knowledge` and `get_published_knowledge`. Customer IDs, order
numbers and knowledge keys are format-checked before any request and every
literal is escaped. Every customer query filters by the trusted customer ID;
another customer's order looks exactly like a missing one. Field lists are
checked against the schema contract when the module loads, so the adapter
cannot select a field the integration user may not read. Expired sessions
are refreshed once; other failures raise typed errors with no secrets and
no fallback data.
