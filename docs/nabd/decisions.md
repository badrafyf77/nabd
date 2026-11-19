# Accepted NABD decisions

Agreed with the user on 18 November 2026. These supersede the old voice roadmap
for active development. Do not reopen settled decisions in later phases unless
new evidence makes a change necessary; record such changes explicitly.

The product/engineering commitments below are settled. Concrete Salesforce
field names, storage mappings, execution budgets, numerical risk thresholds,
and release targets in the plan are initial implementation defaults to validate
against org capabilities and measured scenarios. Record justified adjustments;
they do not require repeating the product interview.

## Product and data

- NABD combines active customer support and complaint intake/analysis in one
  conversation. ShopEasy is its first demonstration business; other businesses
  are future work.
- Preserve the existing synthetic ShopEasy data and scenarios, extending them
  with Moroccan and French customers and multilingual complaints.
- Support English, French, Arabic, and Darija; automatically follow language
  switches, Arabic/Latin-script Darija, and mixed-language messages.
- Languages share ShopEasy's business policies and one currency, GBP. Regional
  rules and currency conversion are outside the initial product.
- Customers use text chat in the existing Next.js application with demo
  sign-in to seeded accounts. The backend binds their identity. Production
  login/registration is deferred.
- Salesforce is authoritative for customers, orders, deliveries, **FAQs and
  policies**, cases, tasks, and management analytics. Knowledge stays editable
  in Salesforce; it is not moved into repository documents as a runtime source.
- Create a dedicated development org. Keep credentials server-side. Synthetic
  seed files remain deployment/test fixtures, not a second live business store.

## Complaints and staff workflow

- Every distinct complaint becomes a case during the conversation once enough
  context exists, even if the assistant ultimately resolves it. Ordinary
  questions do not automatically become complaints.
- Continuing issues update the existing case. Reopen it if the same issue
  returns after resolution; genuinely separate problems get separate cases.
  Retrieve the customer's relevant cases through tools and clarify uncertain
  matches.
- Categories/queues start as Delivery, Orders & Products, Billing & Payments,
  Returns & Refunds, and General Support. Category and sentiment understanding
  uses AI; explicit business policies govern priority and valid actions.
- Sentiment and urgency are separate. Keep uncertainty visible internally;
  ambiguous classifications go to General Support with a review flag.
- Initial statuses: New, Working, Waiting on Customer, Waiting on Staff, Closed.
  A reopened case becomes Working with a reopening recorded.
- Close only with evidence of resolution and customer confirmation. A generic
  thank-you or receipt of tracking information does not establish resolution.
  Staff-dependent actions need actual staff completion as well.
- Refunds initially create staff follow-up cases. Agent-prepared approval
  workflows and refund execution come later.
- Staff use Salesforce, not a separate NABD staff portal or live chat takeover.
  They see English summaries with original customer messages/evidence retained.
- Customer-visible staff case changes and replies appear automatically while
  the chat is open. Support replay/reconciliation after reconnects. Private
  notes and internal risk assessments stay out of customer streams.

## Analytics and visibility

- Salesforce hosts descriptive dashboards and explainable early warnings.
  Important warnings create assigned follow-up tasks. Repeated evaluation
  updates the same active warning/task rather than generating duplicates.
- No trained dissatisfaction/churn predictor in the initial product. Start
  with explicit risk rules, evaluate false positives, and keep evidence.
- Show real-time specialist activity in detail: current action, elapsed time,
  completed work, findings, dependencies, missing information, retries, errors,
  and confirmed outcomes. The active card is expanded and can collapse later.
- Execution hooks generate activity states. Success requires tool confirmation;
  progress is not fabricated by an LLM. Use replayable events, stable IDs,
  localized text, and Arabic RTL support.

## Agent engineering

- Three operational agents: customer-facing Supervisor, read-only Orders
  specialist, and Complaint specialist with structured action proposals. Add a
  read-only background quality evaluator in the quality milestone.
- Use actual model-driven tool calls, typed outputs, authenticated read/write
  MCP servers, useful MCP resources/prompts, and customer-scoped authorization.
  Do not route multilingual meaning with regex or keyword branches.
- Code validates identities, schemas, permissions, priority rules, and lifecycle
  transitions. Models interpret/investigate and supply evidence; validated
  workflows execute mutations.
- Persist checkpoints, messages, pending operations, and activity events. Use
  durable jobs, bounded tool/review loops, idempotent writes, conflict handling,
  traces, and multilingual evaluations covering failures and attacks.
- Shared customer events and restricted engineering traces have correlated
  execution IDs but different server-side projections. Never stream internal
  prompts, private reasoning, credentials, or unrelated records to customers.
- Reuse helpful project components and practices. The old voice roadmap and
  course feature checklist do not govern NABD's release sequence.
- Voice, a generic multi-business platform, production authentication, offline
  notifications, approval/refund execution, and a sandboxed manager analytics
  agent are later work. A new agent needs an evaluated responsibility.

## Recorded implementation adjustments

- **Phase 3 complaint stage order (18 November 2026).** The Complaint
  specialist investigates with its own read tools and runs before the
  Supervisor in each turn, so the Supervisor's reply can cite confirmed case
  receipts. This orders the architecture's turn steps 2–5 as 3, 4, 2, 5; the
  analysis is still mandatory and still uses tool evidence.
- **Phase 3 Salesforce identity (18 November 2026).** The org has one
  Salesforce Integration licence. The read and write MCP servers therefore
  share the integration user. It keeps read-only object permissions and gains
  only execute access to the typed `NabdCaseCommands` gateway (permission set
  `NABD_Integration_Gateway`); direct record writes stay refused. A separate
  write user needs another integration licence.
- **Phase 3 review threshold (18 November 2026).** A category confidence
  below 0.6 routes a new case to General Support with the review flag
  (`NABD_CHAT_REVIEW_CONFIDENCE`), an initial default to evaluate.
- **Phase 3 priority after creation (18 November 2026).** NABD only raises a
  case's priority, and only while staff have not changed it; category and
  owner are set once, on creation. Lowering or re-routing is staff work.
- **Phase 4 knowledge translation (18 November 2026).** Keep one canonical
  published English knowledge revision in Salesforce. The Supervisor translates
  the retrieved content for customers; cards retain the source revision, with
  English record findings labeled. A language switch does not select a different
  policy, change GBP, or create a second policy database.
- **Phase 4 language state (18 November 2026).** Reuse the mandatory Complaint
  specialist's structured result to select turn language/script, including on
  non-complaints. Persist it with the complaint plan and publish enum-only
  language events. Per-message evidence labels are independent of case language;
  code copies the original text and determines its writing systems with Unicode.
- **Phase 4 evaluated defaults (18 November 2026).** The initial paid run used
  Supervisor reasoning `none` and Complaints `low`, and exposed script leakage,
  missed refund cases and repeated-contact errors. The recheck uses the same
  `gpt-5.4-mini` models with Supervisor `low`, Orders `low`, Complaints `medium`,
  stronger language/receipt instructions and an explicit script-preference flag.
  Prompts ask non-English customer replies to use exact ISO values from tools,
  while staff findings retain full English dates plus ISO, avoiding unreliable month/day
  translation. No dates or policy decisions are recomputed by a model.
- **Phase 4 reference and script contracts (18 November 2026).** The paid
  recheck exposed missing reference lookups and Arabic letters in Latin Darija.
  Read the scoped order overview together with case history before classifying;
  save its product/number index for clarification. Models still resolve meaning
  and request details; eligibility is never inferred from the index. A Unicode
  check permits one same-Supervisor format-only rewrite with tools disabled for
  invalid Latin Darija. Rejected drafts stay out of SSE/checkpoints. This adds
  no agent or semantic evaluator and cannot repeat case side effects.

- **Phase order (18 November 2026).** The owner paused Phase 5 part-way
  and moved on to Phase 6. Phase 5's work stays on its own branch until it
  resumes; Phase 6 must not assume staff-to-chat sync exists, and its own
  rule that internal risk never reaches customer streams applies to the
  existing chat streams.
- **Phase 5 Pub/Sub dependency (18 November 2026).** The Salesforce Pub/Sub
  client uses `grpcio` only (pinned 1.85.1) with in-repo decoding of the few
  protobuf messages and the event's Avro record, instead of protobuf,
  fastavro and generated stubs.

- **Latin Darija script repair (18 November 2026).** The focused confirmation
  showed a full-conversation rewrite still producing Arabic letters. The
  default changed from "a second invalid draft fails the attempt" to: one focused
  rewrite given only the draft and its exact Arabic-letter words, accepted only
  if every number survives, then mechanical Arabizi conversion of any remaining
  Arabic letters in code. A customer gets a Latin reply instead of a failed turn;
  the conversion is a writing-system transform and never decides meaning.
- **Phase 6 risk engine location (19 November 2026).** The rules run in
  Salesforce Apex (`NabdRiskEngine`, an asynchronous job after case changes
  and an hourly scheduled sweep), not in a PostgreSQL background job. Every
  rule input and output is a Salesforce record; staff edits must trigger
  evaluation too and the paused staff feed cannot bring them to the backend;
  the customer's account row lock serializes evaluation with case commands;
  and the integration user stays unable to read risk, task and sentiment
  data, so the customer path cannot leak them. The architecture's turn step 6
  ("enqueue risk evaluation") is met by the after-change trigger.
- **Phase 6 counting (19 November 2026).** A complaint is a NABD case
  (`NABD_Issue_Id__c`); cases staff close as duplicates are not distinct
  complaints. A *contact* is one gateway command that adds new customer
  evidence: the gateway stamps its sentiment on that evidence and keeps a
  per-case negative-contact streak (+1 negative, reset otherwise, restarted
  by a reopening). "Unresolved for 24 hours" counts from creation or the last
  reopening (`NABD_Open_Since__c`). Warnings are identified per customer and
  rule code across rule versions; tasks go to the rule's owner; a
  configuration error never clears a warning.
- **Phase 6 brings Phase 5's Salesforce feed into `main` (19 November 2026).**
  The org already had it, and deploying without it fails, so commit `2bb6d02`
  was cherry-picked (`7b4157e`). Its event and triggers stay dormant until
  Phase 5 resumes; rebase the Phase 5 branch on `main` then.

- **Restricted review (19 November 2026).** Commit the completed turn's
  bounded tool trace, model usage and review job together in PostgreSQL.
  Recovered attempts mark their trace partial. Only operators and the
  tool-free evaluator use the trace; the customer event contract is unchanged.
- **One summary correction (19 November 2026).** Key the revision job by the
  summary-writing case command and reuse Complaint capabilities for English
  wording. Use a dedicated `revise_summary` gateway operation so a wording
  change leaves operational case fields and contact accounting unchanged.
  Store the command before sending and settle ambiguous writes by receipt.
- **Receipt schema (19 November 2026).** The existing operation type field is
  a restricted picklist. Add the `revise_summary` value through the schema
  generator. There are no new Salesforce fields or permissions.
