# NABD Salesforce business system

Phase 1 reference, extended in Phases 3 and 6: what lives in Salesforce, how it
is deployed and seeded, how the backend reads it, how it changes cases, and the
risk engine and dashboards. The executable contract is
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
| Complaint | standard `Case` | `NABD_Issue_Id__c` | Category, language, sentiment, urgency, confidence, review flag, English summary, resolution and reopen fields; since Phase 3 NABD's assessed priority and reason, summary hash, staff action required/completed; since Phase 6 the negative-contact streak and the formulas Open Since and Open Age Band |
| Original evidence | `Complaint_Evidence__c` | `Evidence_Key__c` (issue:message) | Master-detail to Case; exact original text; kind = customer report, verified fact or customer confirmation; since Phase 6 NABD's sentiment at that contact |
| Follow-up | standard `Task` | `NABD_Task_Key__c` (matched by query) | Linked by `WhatId` to a Case or Risk Episode; activities cannot hold unique external IDs |
| FAQ/policy | `KnowledgeEntry__c` | `Revision_Key__c` (key@revision) | Draft, Published, Retired revisions of one `Entry_Key__c` |
| Risk rule | `Risk_Rule__c` | `Rule_Key__c` (code@version, set by a trigger) | Three starting rules seeded; the Phase 6 engine uses the highest active version of each code |
| Risk warning | `Risk_Episode__c`, `Risk_Evidence__c` | `Episode_Key__c`, `Evidence_Key__c` | One active warning per customer and rule code (`Active_Key__c`); holds its single follow-up task ID; evidence rows kept as history |
| Operation receipt | `NABD_Operation_Receipt__c` | `Operation_Id__c` | One per case command, written by the gateway with the request hash and result |

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

`Policy_Days__c` holds a policy's number of days where code needs it. The
published `policy.return_window` revision has 30; the read MCP server's return
check uses that value and cites the revision, and reports returns as
uncheckable if no published value exists. Staff change the window by
publishing a new revision with a new value.

## Access

| Who | Access |
| --- | --- |
| Staff (`NABD_Staff` permission set, `NABD_Staff` group) | NABD Support app and tabs; read/create/edit on NABD objects, view all custom records; receipts read-only; queue member of all five queues |
| Backend integration user (`NABD_Integration_Read`, `NABD_Integration_Gateway`, `NABD_Integration` group) | API only; **read-only** on Account, Contact, Case, orders, items, deliveries and knowledge (only the fields the read adapter returns); may run the `NabdCaseCommands` class, which is its only write path; since Phase 6 may publish (and, as Salesforce requires, read) the `NABD_Risk_Check__e` event, which names a customer to re-evaluate and is not a record |

`KnowledgeEntry__c` is private. A criteria sharing rule shares only
`Status__c = Published` revisions with the `NABD_Integration` group, so the
platform itself hides drafts and retired revisions from the backend. The
integration user cannot read internal case fields (sentiment, urgency,
confidence, review flag, description, NABD's priority reasoning) or evidence,
risk and receipt objects. Since Phase 3 it can read the English summary,
reopen, resolution and staff-action fields for case matching; only the
Complaint stage's separately scoped read MCP tool returns them. It still has
no create, edit or delete permission on any object (`verify` checks this).

The backend authenticates through the **NABD_Backend External Client App**
with the OAuth 2.0 client credentials flow, running as the integration user.
Its client ID and secret live only in `backend/.env.nabd` (Git-ignored) as
`NABD_SALESFORCE_CLIENT_ID` and `NABD_SALESFORCE_CLIENT_SECRET`, next to
`NABD_SALESFORCE_DOMAIN_URL`. To get them: Setup → External Client App Manager
→ NABD Backend → Settings → OAuth Settings → Consumer Key and Secret.

## Case command gateway (Phase 3)

`NabdCaseCommands` (hand-written Apex, `salesforce/force-app/main/default/classes/`)
is an Apex REST resource at `/services/apexrest/nabd/v1/case-commands`.
`POST` runs one typed command: `create_case`, `update_case`, `reopen_case`,
`request_closure` or `close_case`; `GET …/{operationId}?customerId=C001`
returns its stored receipt or `unknown`. Each command is one transaction:

1. A receipt with the same `Operation_Id__c` and the same request-body hash
   returns the stored result (`replayed`); a different body is refused.
2. The customer's account is locked; a case must belong to that customer, an
   order to that customer.
3. The case is locked and its `SystemModstamp` compared with the version NABD
   read; a difference is a `conflict` (`version_changed`).
4. The transition is checked: update only open cases (Waiting on Customer
   becomes Working); reopen only closed cases not closed as duplicate (Working,
   reopen count +1, resolution cleared, staff action marked not completed);
   request closure (Waiting on Customer) and close only open cases without a
   pending staff action. Close needs the customer's confirmation as
   `customer_confirmation` evidence and at least one verified fact.
5. The change is applied, evidence is added by `Evidence_Key__c` (existing keys
   are skipped) and the receipt is saved. A concurrent duplicate fails on the
   unique operation ID, rolls back and returns the stored receipt.

Staff overrides win: category and owner are set only on create (owner = the
category's queue from `NABD_Category_Route__mdt`); NABD may raise `Priority`
only while it equals `NABD_Assessed_Priority__c`, and may replace
`English_Summary__c` only while its SHA-256 equals `NABD_Summary_Hash__c`.
Overrides are reported in the result. Staff tick `Staff_Action_Completed__c`
when a refund or other staff-only action is done; until then NABD cannot close
the case. Malformed commands are refused without a receipt. Every query in
the class states `WITH SYSTEM_MODE`: the integration user cannot read evidence
or receipts itself, and an implicit-mode query on them failed at runtime
("Variable does not exist") although the Apex tests, run as an admin, passed.

`NabdCaseCommandsTest` covers creation and routing, replays and refused
reuse, ownership, version conflicts, every transition, staff overrides,
closure rules, evidence de-duplication and refusals. The org has one
Salesforce Integration licence, so the read and write MCP servers share the
integration user; Salesforce itself still refuses that user's direct record
writes.

## Risk engine (Phase 6)

Hand-written Apex, deployed with the rest of `force-app`:

| Piece | Role |
| --- | --- |
| `NabdRiskEngine` | Loads the rules, decides for each customer whether each rule holds, and opens, updates or clears its warning, evidence and task |
| `NabdRiskEvaluation` | Publishes and handles risk checks; the hourly sweep as chained jobs of 50 customers |
| `NabdRiskSchedule` | Hourly at minute 5 (`NABD risk evaluation (hourly)`); queues the sweep |
| `NabdRiskCaseTrigger` | After NABD case changes the rules read (status, priority, account, negative streak, resolution, reopening; delete, undelete), publishes one `NABD_Risk_Check__e` per customer after commit |
| `NabdRiskCheckTrigger` | Evaluates the customers named by risk checks, as the Automated Process user (up to 50 per run; locked rows are retried) |
| `NabdRiskRuleTrigger` | Sets `Rule_Key__c`, refuses unknown codes and invalid parameters, queues a sweep after a definition change |

The three rules, their parameters and the warning lifecycle are described in
the [Phase 6 plan](phases/06-analytics-warnings.md#contracts-and-boundaries).
In short: a warning opens with one task (Not Started, due the next day,
assigned to the rule's owner, type `risk_warning`), is updated in place while
the rule holds, and clears when it does not, completing an open task with a
note. Staff never edit warnings; they work the task. To change a rule, create
version N+1 with new parameters, activate it and deactivate version N.

Why an event: asynchronous Apex runs as whoever started it. Jobs queued from
the gateway ran as the integration user and could not create warnings, and an
event that user publishes without Create permission is dropped silently
(`EventBus.publish` returns a failure, it does not throw). The risk check is
that user's one create right, and its trigger runs as the Automated Process
user. The case gateway records each customer contact's sentiment on the new
evidence and keeps `Negative_Streak__c`. Salesforce refuses to redeploy Apex
that a scheduled job references, so `deploy` pauses the hourly job and
schedules it again afterwards; `configure` schedules it if it is missing.

## Dashboards (Phase 6)

App Launcher → NABD Support → Dashboards → folder **NABD Dashboards**:
**NABD Complaints** and **NABD Risk Warnings**. Their 13 reports are in folder
**NABD Analytics**. Both folders are shared read-only with the `NABD_Staff`
group. The dashboards are dynamic (each viewer's own access) and show at the
top when they were last refreshed; the latest-activity and rule tables show
how fresh the case data and the hourly sweep are. Reports, dashboards and
folders are generated from [`analytics.py`](../../backend/code/nabd/salesforce/analytics.py);
`analytics-reconcile` checks every report against SOQL.

## Fixtures

[`fixtures.py`](../../backend/code/nabd/salesforce/fixtures.py) is the only
fixture source. Seed date **18 November 2026**: every date is an offset from it,
so the same seed date always gives the same records.

| Set | Count | Notes |
| --- | --- | --- |
| Customers | 16 | Legacy C001–C010 unchanged (English); C011–C016 Moroccan/French: 3 Darija, 1 Arabic, 2 French. All live in UK cities because ShopEasy only delivers to the UK |
| Orders / items / deliveries | 35 / 39 / 35 | Legacy 25 orders unchanged; 10 new covering delay, damage, duplicate charge, loss and replacement, refund |
| Knowledge revisions | 24 | 14 FAQs + 8 policies published (legacy facts in written form, e.g. £3.99); `faq.delivery_cost@1` retired; `faq.support_hours@2` draft |
| Cases / evidence / tasks | 8 / 13 / 3 | English, French, Arabic, Darija (Latin and Arabic script) and a French/Darija mix; every status; one closed with customer confirmation; one ambiguous case flagged for review; since Phase 3 assessed priority, reason, summary hash and staff action on each, and FX-ISSUE-0008 (C007, plant pot SE-1101, Waiting on Customer) for a grounded closure; since Phase 6 each customer message carries NABD's sentiment, and FX-ISSUE-0001 (C004) has a second negative message, so it shows the persistent-negative-sentiment warning |
| Risk rules | 3 | Repeat complaints, unresolved High priority, persistent negative sentiment (version 1 of each, active) |

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
| Orders, FAQs/policies, risk and receipt records | Their tab → **All** list view (generated since Phase 2) |
| Risk warnings and their tasks | Risk Episodes tab → **All**; open a warning for its explanation, evidence and task (Phase 6) |
| Dashboards | Dashboards tab → **NABD Dashboards** (Phase 6) |
| FAQs and policies | Also global search (for example "delivery cost") |

Every custom object tab has a generated `All` list view (columns from
`list_columns` in the schema contract; `verify` checks them). Salesforce also
auto-created a queue list view per queue ("Delivery", …) beside the generated
"NABD Delivery", … views; the duplicates are harmless.

## Commands

Run from `backend/` with `PYTHONPATH=code .venv-text/bin/python -m nabd salesforce …`:

| Command | What it does |
| --- | --- |
| `generate` / `check` | Write, or verify, `salesforce/force-app` metadata from the schema contract |
| `deploy --target-org nabd-dev` | Refuse stale metadata, pause the hourly risk job, deploy `force-app`, run local Apex tests, schedule the job again |
| `configure --target-org nabd-dev [--create-integration-user]` | Staff permission set/group for the CLI user; integration user, licence, permission set, group; deploy the client app policy with the run-as username; schedule the hourly risk job |
| `seed --target-org nabd-dev [--seed-date YYYY-MM-DD]` | Create missing fixtures; update unedited fixtures whose definition changed; never overwrite staff edits |
| `reset --target-org nabd-dev --confirm nabd-dev` | Explicitly restore fixture values over staff edits (deletes nothing) |
| `verify --target-org nabd-dev` | Compare live schema, picklists, queues, routes, fixture records, links and access with the contract |
| `smoke` | Read checks as the integration user, using only `backend/.env.nabd` |
| `analytics-reconcile --target-org nabd-dev` | Run every NABD report through the Analytics API and compare its total and groups (or rows) with SOQL (Phase 6) |
| `risk-smoke --target-org nabd-dev` | The warning lifecycle for smoke customer C008 through the real gateway: open, cite evidence, no duplicates, clear, history (Phase 6) |

Case writes are checked with `python -m nabd write-mcp smoke` (15 checks
through the real gateway; one labelled smoke case for C008 that is closed at
the end; reruns only replay receipts).

Fields added to an already-seeded object's mapping are listed in
`seed.ADDED_FIELDS`; an empty value there does not count as a staff edit
against an older fingerprint. The seed stores `NABD_Seed_Fingerprint__c`, a
hash of the values it wrote. A
record whose current values no longer match that hash was edited after
seeding and is reported as preserved. Deployment and seeding use the
developer's `sf` CLI session; the backend never uses that session.

## Read adapter

[`reads.py`](../../backend/code/nabd/salesforce/reads.py) returns typed
results with a `RecordRef` (record ID and `SystemModstamp`) for evidence:
`get_customer`, `list_orders` (with items since Phase 2), `get_order`,
`list_cases`, `list_published_knowledge` and `get_published_knowledge`, plus
`list_demo_customers` for the demo sign-in picker. Agents never call it
directly: they go through the [read MCP server](phases/02-support-chat.md). Customer IDs, order
numbers and knowledge keys are format-checked before any request and every
literal is escaped. Every customer query filters by the trusted customer ID;
another customer's order looks exactly like a missing one. Field lists are
checked against the schema contract when the module loads, so the adapter
cannot select a field the integration user may not read. Expired sessions
are refreshed once; other failures raise typed errors with no secrets and
no fallback data. Phase 3 adds `list_case_history` (cases with summaries and
lifecycle state for matching). Case writes go through
[`commands.py`](../../backend/code/nabd/salesforce/commands.py): typed
`CaseCommand`s with canonical JSON bodies, `CommandResult`s, and server errors
or timeouts raised as `SalesforceUnavailable` (outcome unknown: look up the
receipt before retrying).


## Staff change feed (Phase 5)

`NABD_Case_Change__e` publishes identifiers after commit. `NabdCaseChangeTrigger`
announces relevant Case status/action/ownership/delete/restore changes;
`NabdCaseCommentTrigger` announces public comments, edits and withdrawals.
`NabdCaseCommands` suppresses its own transaction's feed, and the backend also
recognizes its confirmed versions. The integration permission set grants event
Read (subscription) only, without Create (publish) or record write access.
`CaseComment` access follows the case; the backend explicitly queries only
`IsPublished = true` comments and verifies the case's current customer before
projecting anything to a linked conversation. Private notes are never queried.

Staff use the **Case Comments** related list first on the generated Case layout
and mark customer replies public. Status and Staff Action fields drive notices;
no staff portal or live takeover was added. The backend's `staff-sync smoke`
uses CLI-admin staff edits and the real integration subscriber to verify the
loop without OpenAI calls. It closes its synthetic C008 case as a duplicate and
deletes its comments. Stop other subscriber processes using that database first.

The detailed [Phase 5 completion handoff](phases/05-staff-follow-up.md#completion-handoff--19-november-2026)
records recovery/privacy tests, the measured healthy p95 and operating limits.
Salesforce metadata remains generated from `schema.py`; always use
`python -m nabd salesforce deploy` so the hourly Phase 6 risk job is paused and
restored around a deployment.


## Wording-only revisions (Phase 7)

The existing Apex case command gateway accepts `revise_summary` through the
write MCP server. It uses the same account lock, case lock and durable receipt
as the other case commands. The customer must own the case, the version must
match, and the case must have a NABD issue ID and a matching NABD summary hash.
While closed cases are eligible, staff-owned wording is preserved. The
receipt object records the new restricted `Operation_Type__c` picklist value.
The integration user has the same gateway and record permissions as before.

`python -m nabd salesforce risk-review --target-org nabd-dev` reads active
episodes as the CLI admin and compares them with the fixture labels. The C013
24-hour baseline starts from the fixture case's Salesforce creation time.
Test cases or staff edits can produce legitimate differences from the labels.
