# NABD interface contracts

These are design contracts. Phase 2 implemented agent findings
(`nabd/agents/orders.py`), customer activity events (`nabd/runtime/events.py`,
`frontend/src/chat/types.ts`) and the HTTP interfaces below (`nabd/api.py`).
Phase 3 implemented the complaint assessment (`nabd/agents/complaints.py`:
`ComplaintAssessment` from the model, `Assessment`/`Issue` after verification)
and the Salesforce command/result (`nabd/salesforce/commands.py`, the write MCP
tools and the Apex `NabdCaseCommands` gateway). Create concrete
Pydantic models/TypeScript types alongside the first consumers; avoid unused
schema scaffolding. Field names below describe required capabilities and stable
identities; the phase implementation owns their concrete representation.

Phase 6 adds no backend interface. In Salesforce: a gateway command that adds
new customer evidence is one *contact*; its existing `sentiment` argument is
stamped on that evidence and drives `Negative_Streak__c` (results are
unchanged, so the backend never sees it). A risk warning (`Risk_Episode__c`)
is identified by customer, rule code and start; `Active_Key__c` holds
customer:code while active; it owns one Task and keeps `Risk_Evidence__c` rows
with `Current__c`. Rules are `Risk_Rule__c` versions with JSON parameters
checked on save. `NABD_Risk_Check__e` carries one account ID. Reports and
dashboards are defined in `nabd/salesforce/analytics.py`, each with its SOQL
equivalent.

Phase 4 adds a model-selected `language` on the complaint assessment, even for
non-complaints: `input_language` (`en`/`fr`/`ar`/`ary`/`mixed`),
`reply_language` (`en`/`fr`/`ar`/`ary`) and `reply_script` (`latin`/`arabic`).
The model also marks `script_requested` when an explicit response-script
preference overrides the customer's writing system. Otherwise pure Arabic or
Latin source script is preserved for Darija by code. Mixed/neutral source text
keeps the model's choice. The public language object contains only the three
enum values; the preference flag is not customer activity data.
`message_languages` labels only real customer message IDs in the turn history.
Exact message text and Unicode script are copied by code into evidence. Old
stored assessments remain readable; a missing language label stays unknown.

The durable plan stores this selection before dispatch. The public
`conversation.language` event carries only those three enum values, with its
run ID. The snapshot includes the latest `language` independently of the bounded
activity history; each retained run keeps its language for its own cards.
Language metadata never changes authorization, policy or lifecycle decisions.

## Agent findings

- Outcome: done, needs information, or cannot complete.
- Facts with evidence references: Salesforce record/version or tool receipt.
- Relevant order references and unresolved questions.
- Specialists return bounded structured data; only the Supervisor addresses
  customers. Preserve original customer wording when delegating.

## Complaint assessment

- Stable distinct issue reference and supporting customer-message IDs.
- Category, sentiment, impact/urgency evidence, and uncertainty.
- Related orders and authorized existing case candidates.
- Proposed action: create, update, reopen, request closure, gather information,
  or no action.
- English summary and original-language evidence references.
- Reported claims remain distinguishable from tool-verified facts.
- Customer-confirmed resolution links to the issue, confirmation message, and
  actual resolution evidence.

## Salesforce command/result

- A durable operation ID, trusted customer context, target issue/case, and
  expected Salesforce version.
- A narrowly allowed business operation with schema-validated arguments.
- Result: confirmed, pending, failed, or conflict; operation receipt and actual
  Salesforce identifiers/version where available.
- The same operation can be retried/reconciled without a repeated side effect.
- Staff conflict/override is surfaced rather than overwritten.

## Customer activity events

- Event/conversation/run/activity IDs; optional parent activity; monotonically
  ordered sequence and timestamp.
- Event type, activity state, stable localization code, and allowed payload.
- States: queued, running, waiting, retrying, completed, failed. Waiting
  explicitly identifies customer input, dependency, or external operation.
- Show specialist/current action, elapsed time, completed work, authorized
  findings, next action, and confirmed outcome. Never invent percentages.
- Persist important lifecycle events and coalesced message chunks. Replay and
  snapshots reconstruct state without duplicates. Ignore stale run revisions.
- Public payloads exclude raw prompts, private reasoning, credentials, unrelated
  records, internal notes, and dissatisfaction/risk assessments.

## Planned HTTP interfaces (Phase 2 onward)

| Interface | Behavior |
| --- | --- |
| `POST /api/demo/session` | Bind a seeded customer through an HttpOnly session |
| `DELETE /api/demo/session` | End that session |
| `POST /api/conversations` | Create a customer-owned conversation |
| `GET /api/conversations/{id}` | Return authorized messages, activities, case summaries (`cases`: number, team, status, last operation; Phase 3) |
| `POST /api/conversations/{id}/messages` | Accept an idempotent message and return run identity |
| `GET /api/conversations/{id}/events` | Authorized SSE with Last-Event-ID replay |

Phase 2 also serves `GET /api/demo/customers` (sign-in picker),
`GET /api/demo/session` and `GET /api/conversations` (restore after refresh).
SSE events carry `id` = per-conversation sequence; `event: ping` heartbeats
have no id. Use a same-origin browser API path. Backend session context controls all record
and conversation access. Browser disconnects detach the view from durable work;
they do not silently abandon an already submitted Salesforce operation.
