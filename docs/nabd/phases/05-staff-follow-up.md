# Phase 5 — Live staff follow-up

**Status: paused, partly implemented (18 November 2026).** The owner paused
Phase 5 to start Phase 6 first. The work so far is saved on branch
`codex/nabd-phase5-staff-follow-up` (not merged into `main`); see
[Pause handoff](#pause-handoff--18-november-2026). Depends on completed
[Phase 4](04-multilingual.md), checkpoint `b4d5f71` and its handoff commits.

## Outcome

Staff work only in Salesforce. When they change a customer-visible part of a
NABD case (its status, or a staff action such as a refund being completed) or
write a **public** case comment, the customer's open chat shows it
automatically, within five seconds on a healthy connection. Staff replies keep
their original text; a translation into the conversation's language is
clearly labeled as one. Private comments, internal fields and risk
assessments never reach the customer. After any outage (browser, API process,
Pub/Sub stream, missed events) the chat catches up by replay and
reconciliation, without duplicates.

## Starting facts

- Branch `codex/nabd-chat-salesforce` equals `origin/main` at `d92c69c`; no
  local changes; GitHub identity `badrafyf77`.
- Salesforce `nabd-dev` is connected (API 68.0). Docker `db`, `test-db`,
  `read-mcp`, `write-mcp` and `api` (image `nabd-backend:phase4`) are healthy.
- No staff-change feed exists: no platform event, no Pub/Sub client, no
  CaseComment handling. Case metadata comes from `salesforce/schema.py`; the
  base Case layout already contains the Case Comments related list.
- Every NABD case write runs through `NabdCaseCommands`; PostgreSQL
  `nabd.issue_links` records which conversations touched which cases.
- Customer events use `nabd.events` (per-conversation sequence, SSE replay,
  snapshot). A staff update is one more event type on that path.
- Owner decision for this phase (18 November 2026): the Pub/Sub client uses
  **grpcio only** (pinned 1.85.1) with a small in-repo decoder for the few
  Pub/Sub protobuf messages and the event's Avro record, instead of protobuf,
  fastavro and downloaded generated stubs.

## Implementation sequence

1. Record this plan and check the Phase 4 entry criteria.
2. Salesforce: a platform event `NABD_Case_Change__e` published **after
   commit** by triggers on Case and CaseComment for NABD cases. Its payload is
   only identifiers (case, comment, kind of change), never text. Gateway
   transactions publish nothing, so NABD's own writes do not echo. The
   integration user may read (subscribe to) the event; it still has no
   create/edit rights. Staff layout notes; `verify` checks the new pieces.
3. A Pub/Sub API client (`grpcio`) that subscribes with flow control, resumes
   from a stored replay ID, refreshes its token and reconnects with backoff.
4. Migration `0004_staff_sync`: replay cursors, a durable "case needs sync"
   queue, delivered staff replies, and the customer-visible case state per
   linked conversation.
5. The staff sync service: the subscriber stores "case X changed" and the
   cursor in one transaction, then a processor re-reads current authorized
   Salesforce state and appends customer events only for real differences.
   Reconciliation runs after stream reconnects, periodically and when a
   browser opens a conversation.
6. Translation of staff replies into the conversation's language/script as a
   durable follow-up step; the original appears first, the labeled
   translation follows.
7. Snapshot/SSE, agent history (agents see staff replies), the chat UI with
   five template sets, and free tests; then real-org smoke with latency,
   a browser check with scripted models, handoff, checkpoint, merge and push.

## Contracts and boundaries

- **Platform event** `NABD_Case_Change__e` (high volume, publish after commit):
  `Case_Id__c`, `Comment_Id__c`, `Change__c` (`case`, `comment`, `deleted`,
  `undeleted`). Published only for cases with `NABD_Issue_Id__c`. Never carries
  comment text or field values; the event only says "re-read this case".
- **Authorized current state**: the backend re-reads the case (status, staff
  action, owning customer, deletion) and its *public* comments
  (`IsPublished = true`, enforced in the adapter) as the integration user.
  A conversation receives a change only if the case still belongs to that
  conversation's customer. Private comments are never queried.
- **Projection, not forwarding**: customer events are differences between the
  current authorized state and what that conversation was last shown. Repeated,
  replayed or reordered Salesforce events therefore cannot create duplicates.
  A comment reaches a conversation only if it was posted after that
  conversation linked the case.
- **Echoes and NABD's own writes**: the gateway suppresses events for its own
  transactions. A case with a NABD command still in flight is re-synced only
  after the command settles; a current version equal to one NABD's confirmed
  command returned is NABD's own change and updates silently.
- **Cursor**: the replay ID is saved in the same PostgreSQL transaction as the
  durable "case needs sync" marker, so a crash re-delivers at most what was not
  yet durable. An invalid or expired replay ID restarts at the latest event and
  triggers full reconciliation.
- **Customer events** (no run ID): `staff.reply` (reply ID, case number,
  original text, posted time, revision, translation state/text/language),
  `staff.reply.withdrawn`, `case.staff_update` (case number, status, staff
  action state) and `case.removed`. Payloads contain no Salesforce user names,
  internal fields, private comments or risk data.
- **Privacy changes**: an unpublished or deleted public comment is withdrawn
  from every chat that showed it; a case moved to another customer, or
  deleted, is removed from the chat with its staff replies.
- **Translation**: the Supervisor model translates a staff reply with
  structured output and no tools. Code accepts it only if every number of the
  original survives, applies the Latin Darija script repair, and labels it.
  Failures leave the original only, labeled "translation unavailable".
- **Agents**: later turns see staff replies (original text, case number) as
  staff lines; they are never accepted as customer evidence.
- No staff portal, no live takeover, no risk engine or quality evaluator.

## Acceptance and verification

- Gate: the correct customer's open chat receives staff status/action changes
  and public replies automatically; private notes stay restricted; recovery
  after subscriber/API/browser outages works without duplicates; healthy p95
  Salesforce-change-to-customer-event latency is at most 5 s (measured).
- Free tests: protobuf/Avro codecs, the Pub/Sub client against an in-process
  gRPC server, projection rules on real PostgreSQL (echo, in-flight commands,
  edits, withdrawals, re-parenting, deletion, duplicates, cursor crash
  recovery, reconciliation, translation), snapshot/history, UI reducer.
- Real org: deploy with Apex tests, `verify`, read/write MCP smoke, and a
  `staff-sync smoke` that makes staff changes as the CLI admin and measures
  latency through the real Pub/Sub stream.
- Browser check on the real stack with scripted models.
- Paid model runs only with explicit approval.

## Pause handoff — 18 November 2026

Phase 5 stopped after step 3 of the sequence, part-way into step 4. Nothing
was merged into `main`. Resume on branch `codex/nabd-phase5-staff-follow-up`.

### Done and verified

- **Salesforce feed (step 2), deployed to `nabd-dev`.** Schema contract:
  `CASE_COMMENT` (standard object, access follows the case, no object or
  field permissions) and `CASE_CHANGE_EVENT` (`NABD_Case_Change__e`, high
  volume, publish after commit, fields `Case_Id__c`, `Comment_Id__c`,
  `Change__c`). The generator renders event objects and fields, gives only
  the integration permission set read (subscribe) access to the event, and
  puts the Case Comments related list first on the Case layout. Hand-written
  Apex: `NabdCaseChanges` (publisher), triggers `NabdCaseChangeTrigger` (Case
  after update/delete/undelete; only Status, staff action fields, resolution
  outcome or account changes on cases with `NABD_Issue_Id__c`) and
  `NabdCaseCommentTrigger` (public comments, or ones that were public);
  `NabdCaseCommands` sets `NabdCaseChanges.suppressed` for its own
  transactions, so NABD's writes do not echo.
- Deploy with local Apex tests: **25/25 passed** (17 earlier plus 8 in
  `NabdCaseChangesTest`: staff status change, internal-field and non-NABD
  changes silent, staff action completion, public vs private comments,
  privacy change and deletion, case delete/restore, suppression, no gateway echo).
- **Pub/Sub client (step 3)**, `nabd/salesforce/pubsub.py`, `grpcio==1.85.1`
  only (owner decision) with in-repo protobuf and Avro decoding. Against the
  real org as the integration user: `GetTopic` (can subscribe, cannot
  publish), `GetSchema` decoded, and a live `Subscribe` received a public
  comment's event **1.0 s** after commit and its deletion's event **1.2 s**
  after commit; a private note produced no event. The test comments on the
  closed smoke case 00001043 were deleted afterwards.
- `AccessToken.org_id` (the Pub/Sub tenant ID) is parsed from the client
  credentials identity URL and the `sf` CLI session.
- Read adapter `SalesforceReads.case_states(case_ids)`: status, staff action,
  resolution, current owning customer and **public** comments only
  (`IsPublished = true` in the query). Checked as the integration user: the
  smoke case returned its public test comment and not the private one.
- Free tests at the pause: schema, Salesforce client/seed/fixtures and read MCP
  tests **72 passed** (the integration-readable object set now includes
  `CaseComment` and the event). The full suite was not rerun.

### Written but not tested or wired

- Migration `0004_staff_sync` (`sync_cursors`, `case_syncs`, `staff_replies`,
  new `issue_links` columns). Not applied to any database yet.
- `nabd/runtime/staff_projection.py`: the pure projection rules
  (`plan_case_view`) described under Contracts. No tests yet.

### Still to do

1. Store methods (cursor + "case needs sync" marker in one transaction,
   claims with leases and generations, projection transaction, translation
   claims), and the `StaffSync` service: leader-locked subscriber, processor,
   periodic/reconnect/browser-open reconciliation, translation follow-up.
2. `StaffSyncSettings` (`NABD_STAFF_SYNC_*`), app wiring, the staff-reply
   translator (Supervisor model, structured output, number check, Latin Darija
   repair), `{staff_updates}` in the Supervisor prompt and staff lines in the
   Complaint request (never customer evidence).
3. Snapshot/SSE (staff replies and notices placed after the right message,
   removed cases hidden), frontend reducer, rendering and five template sets.
4. Free tests (codecs, client against an in-process gRPC server, projection
   on real PostgreSQL, snapshot/history, UI), `staff-sync smoke` with p95
   latency, `verify` additions (event read access, active triggers), browser
   check, docs (`salesforce.md`, architecture, contracts), then the usual
   handoff, merge and push.

### Facts that matter while Phase 5 is paused

- **The org already has the Phase 5 Salesforce pieces.** Deploying `main`'s
  `force-app` as it is would redeploy `NabdCaseCommands` without the
  suppression, so `NabdCaseChangesTest.gatewayWritesDoNotEcho` (still in the
  org) fails under `RunLocalTests` and the deploy is refused; it would also
  remove the event read access from `NABD_Integration_Read`. Before any other
  Salesforce deploy, bring the Salesforce commit of this branch into the
  working branch (it is complete and tested), or remove those components from
  the org with a destructive deploy.
- The triggers publish events now, but nothing subscribes; this only uses
  platform event allocation. No customer-visible behavior changed.
- `grpcio==1.85.1` is installed in `backend/.venv-text` and pinned on this
  branch only. Docker images were not rebuilt.
- If another phase adds a migration first, renumber `0004_staff_sync` (its
  `down_revision`) when Phase 5 resumes.
