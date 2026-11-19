# Phase 6 — Analytics and proactive warnings

**Status: complete (19 November 2026), implementation checkpoint `45a9445`.**
Depends on completed [Phase 4](04-multilingual.md), checkpoint `b4d5f71`.
Phase 5 is [paused](05-staff-follow-up.md#pause-handoff--18-november-2026);
this phase does not assume staff-to-chat sync exists. See the
[handoff](#handoff--19-november-2026).

## Outcome

Staff open two Salesforce dashboards, **NABD Complaints** and **NABD Risk
Warnings**. They show complaint volume, category, language, sentiment,
priority, recurring (reopened) problems, the age of open complaints and
resolution outcomes, and how fresh the data is. Their totals match the
records exactly.

Explicit, versioned risk rules run inside Salesforce after every relevant case
change and every hour. When a rule holds for a customer, Salesforce opens one
**warning** (a risk episode) that explains itself with the supporting cases and
evidence, and assigns one follow-up **task**. Re-evaluation updates that same
warning; it never creates a second warning or task. A warning that no longer
holds is cleared, with its history kept, and a later recurrence opens a new
warning. Customers never see any of it.

## Starting facts

- Branch `codex/nabd-chat-salesforce` equals `origin/main` at `e0d4f61`; no
  local changes; GitHub identity `badrafyf77`. Offline suite on real
  PostgreSQL at entry: **275 passed**, 101 paid scenarios deselected.
- Salesforce `nabd-dev` is connected (API 68.0); Docker `db`, `test-db`,
  `read-mcp`, `write-mcp` and `api` are healthy.
- The org already has Phase 5's Salesforce feed. Its commit `2bb6d02` is
  cherry-picked into this branch first (`7b4157e`), so deployments from here
  keep it (see the Phase 5 pause handoff).
- Phase 1 created the data structure and nothing else: `Risk_Rule__c` (three
  active version-1 rules, owned by the CLI admin), `Risk_Episode__c`,
  `Risk_Evidence__c` and the Task field `NABD_Follow_Up_Type__c`
  (`risk_warning`). There is no rule engine, report or dashboard.
- The org holds 11 NABD cases (8 fixtures and 3 earlier test cases), all
  created on 18 November 2026. `CreatedDate` is the real creation time, so
  the fixture High-priority case of C013 (00001029, seeded 03:20 UTC) becomes
  a real "High priority unresolved" warning 24 hours after seeding.
- Case sentiment is one current value. Nothing records sentiment per customer
  contact, which the persistent-sentiment rule needs.
- The integration user cannot read sentiment, evidence, risk objects or
  tasks; the backend never reads them.
- Task statuses: `Not Started` (default), `Completed` (the closed status).

## Implementation sequence

1. Record this plan; check the entry criteria; bring the Phase 5 Salesforce
   commit into the branch.
2. Schema contract: per-contact sentiment on evidence and a negative-contact
   streak on cases, open-since and age-band formula fields, engine fields on
   rules, episodes and risk evidence; generator support for formula fields;
   seed mapping for the new fixture values.
3. Case gateway: record the sentiment of each new customer contact and keep
   the negative streak; Apex tests.
4. Risk engine in Apex: `NabdRiskEngine` (rules, findings, warnings, tasks),
   `NabdRiskEvaluation` (asynchronous job for changed customers, or an hourly
   sweep in chained chunks), `NabdRiskSchedule` (hourly), triggers on Case and
   `Risk_Rule__c`; Apex tests for every rule and lifecycle path.
5. Analytics contract `nabd/salesforce/analytics.py`: reports, dashboards and
   folders rendered by the generator. The same definitions produce the SOQL
   that reconciles each report.
6. Commands: `deploy` pauses and resumes the hourly job around deployments;
   `configure` schedules it; `verify` checks the new pieces; new
   `analytics-reconcile` (every report against SOQL) and `risk-smoke` (a
   real-org warning scenario through the real gateway).
7. Check-only deploy, deploy with local Apex tests, configure, seed, verify,
   reconcile and smoke on `nabd-dev`; full offline suite; documentation and
   handoff; checkpoint, merge into `main`, push.

## Contracts and boundaries

### Where the rules run

In Salesforce Apex, not in a backend job (a recorded implementation
adjustment, see [decisions](../decisions.md#recorded-implementation-adjustments)):
every input and output is a Salesforce record; staff edits must also trigger
evaluation, and the paused staff feed cannot bring them to the backend; the
customer's account row lock serializes evaluation with case commands, as in
the gateway; and the integration user stays unable to read risk and
sentiment data, so the customer path cannot leak it. The architecture's turn
step 6 ("enqueue risk evaluation") is met by the after-change trigger.

### Counting

- A **complaint** is a Case with `NABD_Issue_Id__c`: one distinct issue,
  however many messages it gathered. Dashboards and rules count cases, never
  messages. Cases closed by staff as duplicates do not count as distinct
  complaints.
- A **contact** is one case command that adds new customer evidence (a
  customer report or confirmation): one chat turn, however many messages it
  holds. The gateway stamps NABD's sentiment assessment for that contact on
  the new evidence (`Assessed_Sentiment__c`) and keeps
  `Negative_Streak__c` on the case: +1 when the contact is negative or very
  negative, reset to 0 otherwise; a create starts it at 0 or 1, a reopening
  restarts it (a new unresolved period). A replayed command or a command
  without new customer evidence changes nothing.
- **Open since** is the creation time, or the last reopening if later
  (formula `NABD_Open_Since__c`); the age band of open cases is derived from
  it (`NABD_Open_Age_Band__c`).

### Rules (initial defaults from the architecture)

| Rule (version 1) | Parameters | Holds when |
| --- | --- | --- |
| `repeat_complaints` | `{"distinct_complaints": 3, "window_days": 30}` | The customer has at least 3 distinct complaints opened or reopened in the last 30 days |
| `high_priority_unresolved` | `{"priority": "High", "unresolved_hours": 24}` | An open case with that priority has been open for at least 24 hours |
| `persistent_negative_sentiment` | `{"follow_ups": 1}` | An open case's negative streak is at least `follow_ups + 1`: negative at a contact and still negative at the next follow-up |

- Rules are `Risk_Rule__c` records. The engine uses the highest active
  version of each code; another active version records "ignored" in
  `Last_Error__c`. A trigger derives `Rule_Key__c` (`code@version`) and
  refuses unknown codes and invalid parameters when staff save a rule. A
  code with no active version clears its warnings ("rule deactivated").
- Staff change a rule by creating version N+1 and activating it; a warning
  keeps its identity across versions and records the version that last
  evaluated it. Tasks are assigned to the rule's owner.

### Warnings, evidence and tasks

- Warning identity: `Episode_Key__c` = `customer:code:UTC start`.
  `Active_Key__c` = `customer:code` while active (unique, emptied on clear),
  so Salesforce itself refuses a second active warning for one customer and
  rule. Evaluation locks the customer's account row first.
- Holds, no active warning → open one: explanation, one `Risk_Evidence__c`
  per supporting case or evidence record, one Task (Not Started, due the next
  day, related to the warning and the customer's contact, type
  `risk_warning`, key = episode key); the warning stores the task ID.
- Holds, active → update the rule version, explanation, evidence (new rows
  added; rows no longer supporting are kept with `Current__c` false) and
  evaluation time; refresh the task description while the task is open.
  Never a second task, even after staff completed the first.
- No longer holds → clear: status, time, reason; evidence kept; an open task
  is completed with a note saying why. History stays.
- Holds again later → a new warning and a new task (recurrence).
- Explanations are staff-facing English: rule, version, threshold and one
  line per supporting case (number, category, dates, status). They quote no
  customer text; evidence rows link to the original evidence records.
- Engine fields are read-only for staff; staff act on the task.

### When evaluation runs

- After changes: a Case trigger (insert; update of status, priority,
  account, negative streak, resolution or reopening; delete; undelete) on
  NABD cases publishes one after-commit `NABD_Risk_Check__e` per affected
  customer; its trigger evaluates them as the Automated Process user (see the
  handoff for why). Saving a rule queues a sweep. Evaluation is asynchronous,
  so a failure never fails a case command.
- Hourly at minute 5: a sweep of every customer with an open NABD case, a
  NABD case opened or reopened within the longest active window, or an active
  warning, 50 customers per chained job. The last chunk stamps
  `Last_Evaluated_At__c` on the rules (freshness). If platform limits prevent
  queueing after a change, the hourly sweep catches up.

### Dashboards

- Reports in folder **NABD Analytics** and dashboards in **NABD Dashboards**,
  shared read-only with the `NABD_Staff` group. Dashboards are dynamic (each
  viewer's own access), so no org-specific running user is committed.
- NABD Complaints: complaints in the last 30 days, open complaints, reopened
  complaints and active warnings; complaints per day; by category, language,
  sentiment and priority; open complaints by age; resolution outcomes;
  reopened complaints by category; latest complaint activity.
- NABD Risk Warnings: active warnings, warnings by status (history), active
  warnings by rule, the active warning list, and the rules with their last
  full evaluation.
- Freshness: every dashboard shows when it was refreshed; the latest-activity
  table shows the newest case change; the rule table shows the last hourly
  sweep.

### Privacy and scope

- Risk records, tasks, sentiment, the negative streak and evidence are not
  readable by the integration user (`verify` and the risk smoke check this).
  Customer streams and the backend are unchanged.
- No staff-to-chat sync (Phase 5), quality evaluator (Phase 7), trained
  prediction or NABD-side risk interface.

## Acceptance and verification

- Gate: dashboard totals reconcile with SOQL; warnings explain their
  evidence; tasks are not duplicated and clear and recur correctly; internal
  risk does not reach customer streams.
- Apex tests: each rule's threshold, window, priority and streak; duplicates
  and old complaints not counted; one warning and task under repeated
  evaluation; clearing, task completion and recurrence; staff-completed tasks
  not recreated; version changes, deactivation, invalid parameters;
  trigger-to-job and hourly sweep paths; the gateway's contact sentiment and
  streak.
- Free Python tests: generated metadata (formula fields, reports, dashboards,
  folders), analytics contract to SOQL, report-result parsing and
  reconciliation, seed mapping, integration-user field exclusions.
- Real org: check-only deploy, deploy with local Apex tests, `configure`,
  `seed`, `verify`, `analytics-reconcile`, `risk-smoke` (twice, to show
  recurrence), read and write MCP smoke.
- No paid model runs: Phase 6 changes no prompts, models or agent behavior.

## Handoff — 19 November 2026

What works, how it was verified, what was learned, and what is left, in the
order it happened.

### Delivered

- **Risk engine in Salesforce** (`NabdRiskEngine`, `NabdRiskEvaluation`,
  `NabdRiskSchedule`, triggers `NabdRiskCaseTrigger`, `NabdRiskCheckTrigger`,
  `NabdRiskRuleTrigger`): the three version-1 rules from the table above,
  one warning per customer and rule code (`Active_Key__c`), evidence rows
  kept as history, one task per warning, clearing with a reason and a task
  note, recurrence as a new warning, rule versions, refusal of unknown codes
  and invalid parameters on save, and an hourly sweep that stamps each rule's
  last full evaluation.
- **Case gateway**: each command that adds new customer evidence is one
  contact; its sentiment is stamped on that evidence (`Assessed_Sentiment__c`)
  and kept as `Negative_Streak__c` (+1 negative, reset otherwise, restarted
  by a reopening). Replays change nothing. The backend sends nothing new.
- **Risk check event** `NABD_Risk_Check__e` (account ID only, after commit).
  The integration user may publish it (Create, which Salesforce requires
  with Read, on that event only); it still has no record create, edit or
  delete rights and cannot read warnings, tasks, evidence or sentiment.
- **Formula fields** `NABD_Open_Since__c` and `NABD_Open_Age_Band__c`; engine
  fields on rules, warnings and risk evidence, read-only for staff.
- **Analytics** (`nabd/salesforce/analytics.py`): 13 reports in folder NABD
  Analytics, dashboards NABD Complaints (13 components) and NABD Risk
  Warnings (5), both dynamic, shared read-only with `NABD_Staff`, generated
  with the rest of the metadata.
- **Commands**: `analytics-reconcile`, `risk-smoke`; `deploy` pauses and
  resumes the hourly job; `configure` schedules it; `verify` checks triggers,
  schedule, rules, reports, dashboards, one task per warning and the
  integration user's reach.
- **Fixtures**: customer messages carry NABD's sentiment; FX-ISSUE-0001
  (C004) gained a second negative message (FX-MSG-0010), so the seeded data
  shows a real persistent-negative-sentiment warning. The seed now handles
  fields added in several later phases (Phase 3 and Phase 6 on cases).
- Phase 5's Salesforce commit `2bb6d02` is on this branch as `7b4157e`
  (required for any deploy; its event and triggers stay dormant).
- No database migration, no new Python dependency, no prompt, model or
  agent change, no customer-facing change.

### Verification results

| Check | Result |
| --- | --- |
| Offline suite, macOS, real PostgreSQL | **293 passed**, 101 paid scenarios deselected (275 at entry) |
| Check-only deploys while building | Compile and metadata errors fixed one by one (reserved words, report/dashboard rules) |
| Deploy to `nabd-dev` with local Apex tests | Succeeded; **Apex 41/41** (25 before, including Phase 5's 8, plus 1 gateway and 15 engine tests); engine classes 95–100% covered |
| `configure` | Hourly job scheduled (`0 5 * * * ?`); client app re-approved |
| `seed` | 21 writes (8 cases, 12 evidence updated, 1 created), no staff edits preserved by mistake |
| `verify` | **51/51** |
| `analytics-reconcile` | **15/15**: every report's total and groups equal SOQL; the four all-complaint reports agree (11 complaints before the smoke runs) |
| `risk-smoke` (twice) | **17/17** each time, about 40 s; the second run opened new warnings, with history 2 per rule |
| `salesforce smoke`, `read-mcp smoke`, `write-mcp smoke` | **12/12**, **14/14**, **15/15** after the gateway change |
| Dashboards through the Analytics API after a refresh | All 18 components returned data (17 complaints including test complaints, 1 active warning, warning history, 3 rules with freshness) |
| Hourly sweep in the org | Ran as two chained jobs, stamped the three rules, and updated the seeded warning in place with its two customer messages |

### Findings worth keeping

- **Asynchronous Apex runs as whoever started it.** Jobs queued from the case
  gateway ran as the integration user and failed with
  `INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY` when creating warnings,
  although every Apex test passed: tests do not apply that licence's limits.
  Jobs started by the admin worked.
- **Publishing a platform event from Apex needs Create on the event**, and a
  refusal is not an exception: `EventBus.publish` returns a failed result and
  nothing is delivered. The integration user's first risk checks vanished
  this way (the subscriber's position did not move). The fix grants Create
  (with the Read Salesforce requires) on `NABD_Risk_Check__e` only, logs
  refused publishes, and `verify` allows exactly that exception. The event
  trigger then runs as the Automated Process user, which can create warnings
  and tasks for the rule owner.
- Diagnosis used temporary trace flags on the integration and Automated
  Process users; both trace flags and the `NABD_Diagnostics` debug level were
  removed afterwards.
- The seed writes cases before evidence, so the first evaluation after a seed
  can cite only the case; the next sweep adds the evidence lines (seen and
  confirmed with a manual sweep). Gateway commands change both in one
  transaction, so the chat path never shows this.
- Report details: a grouping field may not also be a report column; Lightning
  table components need sort info; dashboard descriptions are limited to 255
  characters. The generator and tests now encode these.

### Known limitations

- Warnings that depend only on time (24 hours unresolved, complaints leaving
  the 30-day window) appear or clear at the next hourly sweep, up to about an
  hour late. The C013 fixture case (00001029, seeded 03:20 UTC on 18
  November) becomes a High-priority warning at the 04:05 UTC sweep on 19
  November.
- The streak counts NABD's assessments at customer contacts; a staff edit of
  Sentiment does not change it. Staff cannot clear a warning by hand; they
  work the task, and the warning follows the records.
- Clearing completes an open task automatically with a note; staff can reopen
  it. A task staff completed is never recreated while its warning is active.
- Explanations are English and quote no customer text; evidence rows link to
  the original evidence records.
- The integration user can publish risk checks (and, as Salesforce requires,
  subscribe to them); they carry account IDs only.
- Developer Edition limits dynamic dashboards; this phase uses two. Daily
  report dates follow the viewer's time zone (the org default is
  America/Los_Angeles).
- Test data in the org: risk-smoke and diagnostic complaints for C008
  (00001045 to 00001049 and those of later runs), closed by "staff" as
  duplicates so no rule counts them, and their cleared warnings. Dashboards
  include them; filter by account to exclude C008.
- Docker images were not rebuilt: the API, MCP servers and chat are
  unchanged. Paid model runs were not needed and none were made.
- Phase 3 and Phase 4 limitations still apply; staff-to-chat sync is still
  Phase 5.

### Resume commands

From `backend/`:

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce deploy --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce configure --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce verify --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce analytics-reconcile --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce risk-smoke --target-org nabd-dev
```

`risk-smoke` writes one or more labelled test complaints for C008 and closes
them as duplicates; each run leaves two cleared warnings as history.

### Next entry criteria

- **Phase 7** (quality review and demonstration release) depends on Phase 6
  and also on Phase 5; resume Phase 5 first unless the owner decides
  otherwise. When Phase 5 resumes, rebase its branch on `main`: Git
  recognises the cherry-picked Salesforce commit, and Phase 5's migration
  `0004_staff_sync` keeps its number because Phase 6 added no migration.
- Deploy with `python -m nabd salesforce deploy`, which pauses the hourly
  job: Salesforce can refuse to redeploy Apex that a scheduled job
  references. (A plain `sf project deploy` with the job scheduled was not
  tried.)
