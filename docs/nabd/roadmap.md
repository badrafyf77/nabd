# NABD phased roadmap

Implementation order: phases 0–7. Complete and verify one phase before starting
the next. Read [START_HERE](START_HERE.md) and [status](status.md) to resume. Phase 0
is complete at `512e03b`; Phase 1 is complete at `1a04f52`; Phase 2 is complete at
`02178fa` (plus handoff commits); Phase 3 is complete at 9879593 (plus
handoff commit); Phase 4 is complete at `b4d5f71`; Phase 5 is complete at `575712a`; Phase 6
is complete at `45a9445`; Phase 7 is implemented on its local branch and the full release gate is pending. [Accepted decisions](decisions.md)
and [architecture](architecture.md) apply to every phase.

## Phase 0 — Foundation

Plan: [00-foundation](phases/00-foundation.md).

- Preserve the existing baseline and create `codex/nabd-chat-salesforce`.
- Record decisions, architecture, interfaces, and phase handoff rules.
- Add independent text startup, pinned speech-free dependencies, settings,
  PostgreSQL migrations/pooling, persistent checkpointing, and CPU-only Compose.
- Gate: reproducible startup; correct health/readiness; real graph/message
  recovery across processes; reusable tests still pass.

## Phase 1 — Salesforce business system

Depends on Phase 0.

Plan and handoff: [01-salesforce](phases/01-salesforce.md) — **complete**, verified
against the real `nabd-dev` org. Reference: [NABD Salesforce](salesforce.md).

- Create the dedicated dev org, External Client App, permissions, and OAuth
  configuration. Package metadata and repeatable seed commands in the repo.
- Implement Account/Contact customers, custom order/item/delivery objects,
  standard Case/Task, versioned FAQ/policy records, evidence, risk, and operation
  receipts. Finalize concrete schema and permission sets here.
- Preserve existing synthetic scenarios; add Moroccan/French customers with
  GBP/shared policies and recorded seed dates. Configure five department queues.
- Gate: staff can inspect the full dataset/knowledge, authenticated backend reads
  work, repeat deployment/seed creates no duplicates or unintended staff resets.
- Handoff: org/deployment instructions, configured identifiers, smoke evidence.

## Phase 2 — Support chat and real-time agent progress

Depends on Phase 1.

Plan and handoff: [02-support-chat](phases/02-support-chat.md) — **complete**; real-model
English run 10/12, then both failures fixed and rechecked.

- Build authenticated read MCP: customer context, orders/deliveries, FAQ/policies,
  resources, and staff review prompt. Use real transport calls from the agents.
- Adapt Supervisor/Orders to Salesforce. Replace voice identity for this flow
  with trusted demo sign-in. Add conversation/message persistence and durable
  graph execution; await dependencies, queue extra turns visibly.
- Implement the planned HTTP/SSE contracts and Next.js chat with expandable
  activity cards, honest lifecycle states, confirmed findings, and replay.
- Gate: English support scenarios succeed through Salesforce/MCP; ownership is
  enforced; progress is visible; refresh/restart restores conversations/events.
- Handoff: usable English support chat and scope/streaming verification.

## Phase 3 — Complaint intelligence and reliable cases

Depends on Phase 2.

Plan and handoff: [03-complaints](phases/03-complaints.md) — **complete**;
real-model English run Phase 3 set 7/8 then the failure passed on recheck; Phase 2 set 9/12 (after two check fixes), then 2 of 3 failures passed on recheck.

- Add mandatory Complaint analysis on every customer turn, multiple distinct
  issues, evidence-backed classifications, and semantic case matching.
- Build write MCP and typed Apex command gateway with transactional operation
  receipts, conflict checks, locks, retry reconciliation, and durable commands.
- Implement agreed creation/update/reopen/closure rules, department mapping,
  priority rules, English summaries, and original evidence. Refunds become
  staff cases. Stream all operations through activity cards.
- Gate: complete English delayed/lost/refund/multiple-issue flows; retries and
  concurrent runs create no duplicates; closure is grounded/customer-confirmed;
  staff overrides are respected.
- Handoff: customer-to-case demonstration and fault-injection results.

## Phase 4 — Multilingual behavior

Plan and handoff: [04-multilingual](phases/04-multilingual.md) — **complete**;
final version 36/36 in one full real-model run.

Depends on Phase 3.

- Support automatic language changes, English/French/Arabic, both Darija scripts,
  and mixed messages. Preserve original wording and staff English summaries.
- Localize activity templates, support RTL, and retrieve published Salesforce
  knowledge through source/version-bearing tools. Clarify ambiguous references.
- Add equivalent scenarios and language-specific held-out challenges; manually
  review Darija examples.
- Gate: all language/forms complete core support/case flows without changes to
  ownership, issue identity, or policy outcomes; browser presentation works.
- Handoff: language-specific measured results and reviewed fixtures.

## Phase 5 — Live staff follow-up

Depends on Phase 4.

Plan and handoff: [05-staff-follow-up](phases/05-staff-follow-up.md) —
**complete** (19 November 2026), resumed after Phase 6 on branch
`codex/nabd-phase5-staff-follow-up`, rebased onto it. Real staff loop 12/12;
ten-change p95 2.696 s to durable customer event; browser loop verified.

- Finish staff layouts, evidence access, public responses, and case statuses.
- Publish after-commit Salesforce events and consume them via Pub/Sub with
  durable cursors, authorized current-state retrieval, and public projection.
- Handle privacy changes/deletion, deduplication, reconnect reconciliation,
  missed events, and clearly labeled translations of original staff replies.
- Gate: correct customer's open chat receives staff changes automatically;
  internal notes stay restricted; recovery works; healthy p95 latency <=5s.
- Handoff: complete staff/customer loop and event-recovery evidence.

## Phase 6 — Analytics and proactive warnings

Depends on Phase 5. The owner chose to start it while Phase 5 was paused
(18 November 2026); Phase 5 was then resumed and completed after Phase 6.

Plan and handoff: [06-analytics-warnings](phases/06-analytics-warnings.md) —
**complete** (19 November 2026); real org Apex 41/41, reports reconciled
15/15, warning lifecycle smoke 17/17 twice.

- Build Salesforce dashboards: volume/category/language/sentiment/priority,
  recurring problems, age, and resolution outcomes; display data freshness.
- Implement versioned configurable risk rules after changes and hourly for
  aging. Initial rules/defaults are in architecture; validate with fixtures.
- One follow-up task per active customer/rule episode; update evidence, clear
  inactive warnings, preserve history. Count distinct issues, not messages.
- Gate: totals reconcile, warnings explain evidence, tasks deduplicate and
  resolve/recur correctly, internal risk does not leak to customer streams.
- Handoff: verified dashboards and risk/task scenarios.

## Phase 7 — Quality review and demonstration release

Depends on completed Phases 5 and 6. See the [Phase 7 plan and evidence](phases/07-quality-release.md).
Testing accompanies every phase.

- Add read-only background evaluator and grounded summary review with at most
  one corrective pass. Grade coverage, language, tool use, and resolution claims.
- Run complete multilingual scenarios three times, held-out challenges, browser
  checks, failures/recovery, authorization, privacy, and prompt-injection cases.
- Measure latency/cost, case accuracy, risk false positives, and sync lag. Deliver
  operations/runbook, reproducible setup, scripted demo, and follow-up backlog.
- Gates: deterministic authorization/lifecycle/replay/idempotency tests all pass;
  no disclosure or repeated side effects in fault tests; >=90% scenario success
  per language/form across three runs; p95 event-to-browser <=1s and healthy
  staff updates <=5s. These are targets to measure, not current claims.

## Phase completion and resumption

For each phase save its detailed plan and a handoff recording what works, how to
demonstrate it, test results, limitations, environment requirements, Git checkpoint,
exact restart commands, and next entry criteria. Update status, START_HERE, and
the root AGENTS checkpoint pointer together. Create a local Git checkpoint,
then merge the finished phase into `main` and push `main` and the phase branch
to `origin` as `badrafyf77` (the repository owner's standing instruction since
18 November 2026). Unfinished work is not merged. Do not deploy external
resources merely to mark a phase complete.

"Start Phase N" means read status/decisions/that phase plan, inspect current
repository state, verify prerequisites, then implement that phase only. Carry
forward settled decisions. Create concrete contracts with real consumers and
test each full workflow before broadening it.

## Deferred

Voice development, production authentication, multi-business support, regional
currencies/policies, approval/refund execution, offline notifications, trained
churn prediction, and a sandboxed open-ended manager analytics agent.
