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
