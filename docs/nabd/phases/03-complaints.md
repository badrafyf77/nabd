# Phase 3 — Complaint intelligence and reliable cases

**Status: complete (18 November 2026).** Depends on completed
[Phase 2](02-support-chat.md), checkpoint `02178fa` plus handoff commits. The
handoff at the end of this page records what was delivered and verified.

## Outcome

Every customer message is checked by a Complaint specialist. A real complaint
becomes a Salesforce case during the chat (or updates, reopens or closes the
customer's existing case for the same problem); ordinary questions do not.
Cases are written only through a typed Salesforce command gateway behind an
authenticated write MCP server, exactly once even when a run is retried or two
conversations of the same customer run at the same time. The customer sees
each case step live and is told a case number only after Salesforce confirmed
it. Staff changes in Salesforce win over NABD's.

Read [accepted decisions](../decisions.md) first, especially
[complaints and staff workflow](../decisions.md#complaints-and-staff-workflow).
The [roadmap](../roadmap.md), [architecture](../architecture.md) and
[contracts](../contracts.md) give the target; this page fixes Phase 3's choices.

## Starting facts (checked 18 November 2026)

- Offline suite 213 passed (57 paid deselected) against real PostgreSQL;
  `nabd-dev` connected (API 68.0); dev and test databases running.
- The org has **one** Salesforce Integration user licence and it is used by
  the read-only integration user. A second API-only user is not possible
  without another licence (see "Salesforce identity" below).
- Case fields for NABD analysis, `Complaint_Evidence__c`,
  `NABD_Operation_Receipt__c` (empty), five queues and their routes exist.
  The integration user can read only public case fields and cannot write.
- The Supervisor tells customers it cannot open cases or arrange refunds;
  that must change once cases exist.

## Implementation sequence

1. Write this plan.
2. Salesforce: new Case fields, case-history fields readable by the
   integration user, one more English fixture case for a grounded closure,
   the Apex command gateway with Apex tests, a gateway-only permission set.
   Regenerate, deploy, configure, seed, verify.
3. Python adapters: `list_case_history` read; the gateway client.
4. Read MCP: `list_case_history`, allowed only with a separate scope.
5. Write MCP server: named case operations over the gateway.
6. Complaint specialist (model, read tools, typed assessment) and the
   validated case workflow (code: priority, routing, lifecycle, commands).
7. Durable commands and issue links in PostgreSQL; the turn graph runs the
   complaint stage before the Supervisor replies; background reconciliation.
8. API snapshot case summaries; activity codes and texts; frontend cards.
9. Tests (offline, PostgreSQL, Apex, real-org smoke), browser check, and a
   paid English run only after the user approves it. Docs and checkpoint.

## Technical defaults chosen for Phase 3

### Turn sequence

The checkpointed per-conversation graph becomes
`complaints -> supervisor model <-> tools`:

1. **Complaint stage** (one graph node, always runs). It takes the
   customer's per-customer case lock, calls `list_case_history` itself
   (deterministic retrieval of the authorized candidate cases), then lets the
   Complaint specialist investigate with `list_orders`/`get_order` and return a
   typed assessment. Code verifies the assessment, turns it into commands,
   stores them in PostgreSQL, dispatches them through the write MCP server,
   records the results and releases the lock.
2. **Supervisor**: as in Phase 2, but its system prompt gets a "case work for
   this message" section built from confirmed command results, pending or
   failed operations, clarification needs and the specialist's verified facts.

The specialist investigates with its own read tools, so the complaint stage
can run before the Supervisor and the reply can cite confirmed receipts. This
orders architecture steps 2-5 as 3-4-2-5; the Supervisor still investigates
with its own tools and the Orders specialist when the reply needs more.

### Complaint specialist

- Model `gpt-5.4-mini`, reasoning `low` (`NABD_COMPLAINTS_MODEL`,
  `NABD_COMPLAINTS_REASONING_EFFORT`). Tools: `list_orders`, `get_order`
  (5 per invocation, shared 20 per turn). Never talks to the customer.
- Input: the last 12 conversation messages with their stored message IDs,
  the current message, and the case-history result.
- Output (structured): `no_complaint`, `needs_clarification` (question) or
  `issues`. Each issue: matched case number or new, match certainty, proposed
  action (create, update, reopen, request_closure, close, gather_information,
  no_action), category and confidence, sentiment, urgency with its reason,
  priority signals, staff action needed, order numbers, supporting customer
  message IDs, reported claims, verified facts with evidence references,
  English subject and summary, and for closure the confirmation message ID.
- Code keeps only verified facts whose references came from this turn's tool
  results, message IDs of real customer messages in this conversation, order
  numbers seen in tool results, and case numbers from the candidate list.

### Validated case workflow (code, `nabd/agents/cases.py`)

- **Matching**: an uncertain match creates nothing; the customer is asked.
  A matched closed case with an update/reopen proposal is reopened; a matched
  open case with a reopen proposal is updated; no match means create.
- **Routing**: category confidence below 0.6 (initial default) goes to
  General Support with `Needs_Review__c`; otherwise the category's queue.
- **Priority policy**: High for reported safety/security incidents and
  suspected unauthorized or duplicate charges; Medium for lost parcels and
  repeated unresolved issues (always on reopen); Low otherwise. The reason is
  stored on the case. NABD only raises priority, and never after staff
  changed it.
- **Refunds** become Returns & Refunds staff cases with staff action
  `refund`; NABD never executes a refund.
- **Closure**: `close` needs a confirmation message from the customer in
  this conversation, at least one verified resolution fact from this turn,
  and completed staff action where one was required. `request_closure`
  (verified resolution, no confirmation yet) sets Waiting on Customer and the
  Supervisor asks the customer to confirm. A thank-you alone is not a
  confirmation (prompt rule; the gateway still requires the evidence).
- **Evidence**: exact text of the supporting customer messages (kind
  customer report, or customer confirmation for closures) and verified facts
  with their Salesforce reference, keyed `issue:message` / `issue:fact:<hash>`
  so a repeat never duplicates evidence.
- **Conflicts**: a version conflict re-reads the case once; if the same
  action is still valid for the new state it is sent once more with the new
  version, otherwise the conflict is reported.

### Salesforce command gateway (Apex)

- `NabdCaseCommands`, an Apex REST resource at
  `/services/apexrest/nabd/v1/case-commands`: `POST` runs one command,
  `GET …/{operationId}` returns its receipt. Operations: `create_case`,
  `update_case`, `reopen_case`, `request_closure`, `close_case`.
- One transaction per command: find the receipt by `Operation_Id__c` (same
  request body hash -> stored result as a replay; different body -> refused);
  check the customer and that the case or order belongs to them; lock the case
  (or the customer's account for creates); compare `SystemModstamp` with the
  expected version; check the transition; apply it; upsert evidence by key;
  save the receipt. The unique operation ID makes a concurrent duplicate fail
  and roll back; it then returns the stored receipt.
- Transitions: update only open cases (Waiting on Customer becomes Working);
  reopen only closed cases not closed as duplicate (becomes Working, reopen
  count +1, resolution cleared); request closure and close only open cases
  without pending staff action; close sets Closed and the resolution fields.
- Staff overrides: category and owner are set only on create. Priority
  changes only while `Priority` still equals `NABD_Assessed_Priority__c`. The
  English summary is replaced only while its hash equals
  `NABD_Summary_Hash__c`. Overrides are reported in the result.
- Results: `confirmed`, `conflict` (with a reason code) or `failed`, plus
  case ID, number, version, status and queue. Business outcomes are saved as
  receipts; malformed requests are refused without one.

### Salesforce identity

The single integration licence is shared. The integration user keeps
**read-only object permissions** (direct record writes stay refused, which the
Salesforce smoke checks). A new permission set `NABD_Integration_Gateway`
grants it only access to the `NabdCaseCommands` class, which runs in system
mode and enforces every rule above. Only the write MCP server calls the
gateway; the read MCP server has no write code. A separate Salesforce user per
MCP server needs another integration licence and is recorded as a limitation.

New Case fields: `NABD_Assessed_Priority__c`, `NABD_Priority_Reason__c`,
`NABD_Summary_Hash__c`, `Staff_Action_Required__c` (picklist: refund,
replacement, billing review, carrier investigation, other) and
`Staff_Action_Completed__c` (ticked by staff). The integration user can also
read `English_Summary__c`, reopen and resolution fields and the staff-action
fields for case matching. Sentiment, urgency, confidence and review flag stay
unreadable to it. Fixture cases get their assessed priority, summary hash and
staff action through `seed.ADDED_FIELDS`.

### Read and write MCP

- Read MCP gains `list_case_history` (summaries, reopen and staff-action
  state, evidence references). It requires scope `nabd.read.case_history`;
  the Supervisor's and Orders' session token has only `nabd.read`, the
  complaint stage opens its own session with both.
- Write MCP (`python -m nabd write-mcp serve`, port 8092, `/mcp`, Compose
  service `write-mcp`): audience `nabd-write-mcp`, scope `nabd.write.cases`,
  its own secret `NABD_WRITE_MCP_CONTEXT_SECRET`. Tools `create_case`,
  `update_case`, `reopen_case`, `request_case_closure`, `close_case`,
  `get_operation`. The customer and conversation come only from the token.
  Models never see these tools; the case workflow calls them.

### Durable commands (PostgreSQL migration `0003_case_commands`)

- `case_commands`: operation ID, run, conversation, customer, position, issue,
  operation, exact request, status (`pending`, `dispatched`, `unknown`,
  `confirmed`, `conflict`, `failed`), attempts, result, public card payload.
  `run_complaints`: the verified assessment of each run. `issue_links`: the
  cases each conversation created or changed (for the snapshot).
- The plan and its commands are stored in one transaction before anything is
  sent. A retried run reuses them instead of asking the model again, so
  operation IDs and evidence keys are stable.
- A timeout or transport error makes a command `unknown`. It is reconciled
  (receipt lookup, then the same operation again) before any retry. The worker
  reconciles leftover unknown commands in the background and updates the same
  activity card. Case mutations of one customer are serialized with a
  PostgreSQL advisory lock held during the complaint stage, so a concurrent run
  sees the other run's new case and matches it.

### Activity and API

- Codes `agent.complaints`, `tool.list_case_history`, `case.create`,
  `case.update`, `case.reopen`, `case.request_closure`, `case.close`. A case
  card waits on `external_operation` while Salesforce works and shows the case
  number, team and status only after confirmation. Payloads never contain
  priority, sentiment, urgency, confidence or summaries.
- `GET /api/conversations/{id}` adds `cases`: the conversation's confirmed
  case numbers, team and last status.

## Verification gate

- Apex tests: create/replay/refused reuse, customer and order ownership,
  version conflict, transitions, staff action pending, priority and summary
  overrides, evidence deduplication.
- Offline Python: gateway client, write MCP over Streamable HTTP with a fake
  gateway, assessment verification, priority/routing/lifecycle rules, complaint
  flows with scripted models (delayed, lost matching an existing case,
  refund, two issues in one message, ordinary question, thank-you without
  closure, confirmed closure, reopen, uncertain match).
- PostgreSQL: a crash after dispatch retries without a duplicate case; an
  ambiguous timeout is reconciled; two conversations of one customer at once
  produce one case; staff overrides survive.
- Real org: deploy with Apex tests, `verify`, Salesforce and read-MCP smoke,
  write-MCP smoke (idempotent, conflicts and refusals).
- Browser: case cards on a scripted turn against the real write path.
- Paid English scenarios only after the user approves the run size.

## Phase boundary

Other languages and RTL are Phase 4; staff-to-customer live updates
(Pub/Sub) are Phase 5; risk warnings and tasks are Phase 6; the quality
evaluator and post-response summary refinement are Phase 7 or later. Refund
execution stays deferred.

## Handoff — 18 November 2026

Git checkpoint: `987959317beae7339f3bb51958032df0cb427b45` (*Build NABD complaint cases with a Salesforce command gateway (Phase 3)*) on `codex/nabd-chat-salesforce`.

### Delivered

- **Salesforce**: Case fields `NABD_Assessed_Priority__c`,
  `NABD_Priority_Reason__c`, `NABD_Summary_Hash__c`, `Staff_Action_Required__c`,
  `Staff_Action_Completed__c` (layout section "NABD Staff Action"); English
  summary, reopen, resolution and staff-action fields readable by the
  integration user; generated permission set `NABD_Integration_Gateway`
  (execute `NabdCaseCommands` only); the Apex gateway and 13 Apex tests;
  fixture case FX-ISSUE-0008 (C007, SE-1101) and Phase 3 values on the seven
  existing cases through `seed.ADDED_FIELDS` (an unticked new checkbox now
  counts as empty there). `verify` gained gateway-access and no-write-rights
  checks.
- **Python Salesforce layer**: `list_case_history` read; `commands.py` with
  typed commands, canonical bodies and results; `raw_json` requests.
- **Read MCP**: `list_case_history`, refused unless the context carries
  `nabd.read.case_history`; smoke checks for both cases.
- **Write MCP** (`nabd/write_mcp/`): six tools (`create_case`, `update_case`,
  `reopen_case`, `request_case_closure`, `close_case`, `get_operation`), own
  audience/scope/secret, schema-checked arguments, customer and conversation
  from the token, `[unavailable]` for ambiguous outcomes; `serve` and `smoke`
  commands; Compose service `write-mcp`.
- **Agents**: Complaint specialist (`complaints.py`, prompt `complaints.md`)
  and the validated case workflow (`cases.py`): verification, matching and
  lifecycle rules, routing with review, priority policy, refund staff cases,
  evidence, durable plan, send/lookup/resend, one conflict re-read, per-customer
  lock and background reconciliation. The Supervisor graph runs the stage
  first and its prompt lists the confirmed case work; the "cannot open cases"
  rule is gone.
- **Runtime**: migration `0003_case_commands` (`run_complaints`,
  `case_commands`, `issue_links`); store methods; three MCP sessions per run
  (read, case-history read, write); `RunWorker.reconcile_once`; snapshot
  `cases`.
- **Frontend**: texts for `agent.complaints`, `tool.list_case_history` and the
  five `case.*` codes, failure reasons, the "other conversation" wait, a
  confirmed-case line on case cards and a case strip under the header.

### Verification results

| Check | Result |
| --- | --- |
| Offline suite, macOS, real PostgreSQL | **249 passed**; 65 paid scenarios deselected (45 legacy, 12 Phase 2, 8 Phase 3) |
| Linux test image | **240 passed, 9 skipped** (need `salesforce/`), 65 deselected |
| New offline tests | complaint flows and rules (23), write MCP and gateway client (6), PostgreSQL case work (5), case-history scope (1), gateway permission set (1) |
| Salesforce deploy with local Apex tests | Succeeded; **Apex 17/17** (13 gateway) |
| `salesforce verify` | **41/41** (new: gateway class access, no object write rights) |
| Seed after the new fields / repeated | 7 cases updated, 1 case and 2 evidence created, nothing preserved; then **0 writes** |
| `salesforce smoke` | 12/12 (direct writes still refused) |
| `read-mcp smoke` | 12/12 (seven tools; case history refused without its scope, scoped with it) |
| `write-mcp smoke` against the org | **15/15**; the rerun replayed every receipt: 1 smoke case, 7 receipts |
| Browser (scripted models; real API, both MCP servers, Salesforce, dev PostgreSQL) | C006 complaint opened case 00001044 in the Delivery queue; live cards complaint check → case history → order → "Waiting for Salesforce to confirm" → confirmed case line; follow-up updated the same case; reload restored turns, cards and the case strip; Salesforce held both original messages, one de-duplicated fact and two receipts |
| Docker Compose (`nabd-backend:phase3`) | migrate to 0003; `read-mcp`, `write-mcp`, `api` healthy; `/readyz` ready |
| Frontend `tsc` | Clean |
| Real-model scenarios (`-m llm`, once each, `gpt-5.4-mini`) | Phase 3 set **7/8**, the failure passed its recheck; Phase 2 set as full turns **7/12**, **9/12** after two over-strict checks were corrected, then 2 of the 3 remaining failures passed their recheck (details below) |

### Real-model run — 18 November 2026

Approved by the user: all 20 scenarios once (8 new in
`tests/nabd/test_case_scenarios.py`, the 12 Phase 2 ones in
`test_chat_scenarios.py` run as full Phase 3 turns), then a recheck of the
failures. Orders and knowledge came from the real org, cases from the
in-memory gateway (`tests/nabd/live_turns.py`).

**Phase 3 set** (first prompt version): case commands were right in all 8.

| Scenario | Result | What happened |
| --- | --- | --- |
| delayed_delivery_opens_a_case | **Fail**, then pass | Case created in Delivery; the reply gave the new date but no case number. After the prompt fix: "I've recorded this as case 00002001 with our Delivery team." |
| lost_parcel_updates_the_existing_case | Pass | Updated existing case 00001001; reply named it (also mentioned refunds unasked) |
| refund_request_becomes_a_staff_case | Pass | Returns & Refunds case with staff action refund; "I can't arrange the refund myself … case 00002001" |
| two_problems_open_two_cases | Pass | Two cases (Delivery, Orders & Products); reply did not name them |
| a_question_needs_no_case | Pass | No case; £3.99 |
| an_on_time_order_needs_no_case | Pass | No case; in transit, expected 20 November |
| thanks_is_not_a_closure | Pass | Turn 1 updated 00001008; "Ok, thanks." sent no command |
| a_confirmed_resolution_closes_the_case | Pass | Closed 00001008 with the confirmation and the delivery record |

**Prompt fix 1** (before the Phase 2 set): the Supervisor must tell the
customer the case number and team of confirmed case work, and must not bring
up refunds unless the message asks for one.

**Phase 2 set as full turns**: 7/12. Two checks were too strict and were
corrected, re-scoring the recorded answers without new calls: `late_delivery`
(correct, with the case number, but used the complaint stage's verified facts
instead of delegating to Orders) and `non_returnable_item` ("aren't
returnable"). The three real misses:

| Scenario | First run | Recheck after prompt fix 2 |
| --- | --- | --- |
| refund_request | No case was opened (in the Phase 3 set the same sentence opened one) | **Pass**: case with Returns & Refunds, "I can't arrange the refund myself" |
| ask_for_human | Gave the contacts without saying it can't transfer | **Pass**: "I can't transfer this chat to a person" plus contacts |
| several_orders_needs_info | Asked "which item?" without consulting Orders | **Fail** against the expectation: Orders answered `done` and the reply listed both returnable items with deadlines instead of asking (a correct answer; the check expects a question) |

**Prompt fix 2**: complaint examples (a refund request is a complaint even
when the item is returnable), "ask Orders even when no order is named", and
"say you can't transfer the chat" for requests for a person.

The other scenarios were not rerun after the fixes; each result is a single
observation, not a success rate. The re-run of `several_orders_needs_info` was
a second recheck the user approved after the first recheck's output was lost
by a log filter.

### Findings worth keeping

- Apex SOQL without `WITH SYSTEM_MODE` failed at runtime for the integration
  user on objects it cannot read ("Variable does not exist: tmpVar1"), while
  the same code passed Apex tests as an admin. State the mode on every query.
- The MCP SDK ignores unknown tool arguments rather than rejecting them;
  customer identity therefore comes only from the token, never from arguments.
- Store the exact write-tool arguments (including the operation ID) before
  sending; a retry must send identical bytes for the gateway's body hash.
- An admin probe of the gateway created case 00001042 (closed as withdrawn);
  the browser check created 00001044 (closed as withdrawn); the write smoke
  owns 00001043 (closed). All belong to test customers' histories.

### Known limitations

- The read and write MCP servers share one Salesforce integration user (one
  licence); Salesforce does not separate their credentials.
- Paid scenarios use real Salesforce reads but the in-memory gateway for
  cases, for repeatability; the real write path is proven by the write smoke
  and the browser check, not by real-model runs.
- Complaint analysis runs before the Supervisor and holds the customer's case
  lock during the specialist's model calls, so a second conversation of the
  same customer waits (shown on its card).
- A command that stays unknown after the run shows a waiting card until the
  background pass settles it; after five attempts it fails.
- Real-model evidence is single runs; scenarios not rechecked after the two
  prompt fixes ran on earlier prompt versions. The specialist's decision for
  the same sentence varied between runs (a refund request once got no case).
- No post-response summary refinement, risk evaluation or staff-to-chat sync
  yet (later phases). English prompts only; evidence script is not detected.

### Resume commands

From `backend/`, with both MCP secrets and `NABD_OPENAI_API_KEY` in `.env.nabd`:

```bash
docker compose -f docker-compose.nabd.yml up -d --build --wait api
PYTHONPATH=code .venv-text/bin/python -m nabd write-mcp smoke
cd ../frontend && npm run dev   # http://localhost:3000
```

Paid checks, only with the user's approval:

```bash
cd backend && .venv-text/bin/python -m pytest -m llm tests/nabd/test_case_scenarios.py -s
```
