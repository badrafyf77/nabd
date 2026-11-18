# Phase 4 — Multilingual behavior

**Status: complete (18 November 2026), implementation checkpoint `b4d5f71`.** Depends on completed
[Phase 3](03-complaints.md), checkpoint `9879593` and its handoff commits.

## Outcome

Customers use English, French, Arabic, Darija in Arabic or Latin script, and
mixed messages, switching naturally in one conversation. The same Salesforce
ownership, policy outcomes and case identity apply in every language. Staff
receive English subjects/summaries and the exact original customer evidence.
Activity templates follow the model-selected language/script and render RTL
where appropriate, including after replay and refresh.

## Starting facts

- Working branch `codex/nabd-chat-salesforce` equals `origin/main` at `603545d`.
  No local changes at entry; GitHub identity is `badrafyf77`.
- Salesforce `nabd-dev` is connected; Docker databases and Phase 3 services
  are healthy. Prerequisite offline and MCP/org checks are run before changes.
- Complaint analysis already runs on every turn; use its structured output
  for language selection instead of adding a fourth model or intent heuristics.
- Published knowledge is English, versioned and retrieved from Salesforce.
  Translating retrieved content keeps a single authoritative policy revision.

## Implementation sequence

1. Record this plan and verify the Phase 3 prerequisites.
2. Add typed turn language/script to complaint analysis, persist it with the
   durable plan, and publish a replayable language event before case writes.
3. Preserve each evidence message's own language and script; keep exact text,
   English staff summaries, scoped reads and existing command rules.
4. Update all three prompts for language switches, cross-language matching,
   original-wording delegation, ambiguity, and faithful knowledge translation.
5. Localize activity, run, case and chat chrome templates; apply language and
   direction from durable events, isolate identifiers, retain source revisions.
6. Add equivalent core scenarios and held-out challenges for each language/form,
   free invariant/replay tests, and browser checks on the real stack.
7. Run paid evaluations only with explicit approval. Review Darija fixtures,
   record measured results and limitations, update handoff/status/entry points,
   secret-scan, checkpoint, merge into main and push both branches.

## Contracts and boundaries

- Turn language: `en`, `fr`, `ar`, `ary`, or `mixed`; reply language is one of
  the four supported languages; script is `latin`, `arabic`, or `mixed`.
  The model follows explicit preferences, otherwise the current message, using
  prior conversation to disambiguate short replies and mixed messages.
- Language metadata has no authority over customer identity, permissions,
  policies, issue matching, or case lifecycle. No keyword/regex intent routing.
- The existing complaint-plan JSON and graph checkpoints own language state;
  a `conversation.language` event makes it available to SSE/snapshots. No new
  database tables or business data fallback.
- Original evidence is copied by message ID from persisted text, never from a
  model rewrite. Per-message model language labels are validated against those
  IDs; script is determined from Unicode writing systems, not business meaning.
- Knowledge continues through published Salesforce read tools/resources with
  source/version references. Customer answers translate meaning while preserving
  exact order/case references, amounts, dates and policy decisions. Staff-only
  English findings are labeled as such when shown in localized cards.
- No staff-event synchronization, risk engine or quality evaluator (Phases 5–7).

## Acceptance and verification

- Core support/knowledge/refund/case flows exist for all six language/forms.
  Cross-language follow-up keeps the case number; ambiguous references ask;
  generic thanks do not close cases; ownership and return outcomes stay fixed.
- Free scripted tests exercise metadata validation, exact evidence and script,
  durable retry, command invariants, language-event replay and UI templates.
- Real PostgreSQL suite, integration-user Salesforce/read-MCP/write-MCP smoke,
  frontend type/build checks and RTL/Latin browser presentation are recorded.
- Paid scenarios are separately measured, once per approved run; failures and
  rechecks remain visible. Scripted tests do not establish language-model quality.
- Darija fixtures receive an explicit linguistic review; any remaining need for
  native-speaker confirmation is recorded rather than presented as verified.

## Handoff

Implementation, commands, measured results, limitations, checkpoint and Phase 5
entry criteria, in the order they happened.

### Implementation and free verification — 18 November 2026

- Three prompts now follow language/script changes, semantic references and
  cross-language case identity. Supervisor delegation carries exact original
  wording; staff findings remain English. Salesforce knowledge stays one set
  of published English revisions, translated when answering.
- `nabd/agents/language.py`, complaint assessment/plan JSON, the public language
  event, snapshot and frontend reducer implement the language contract above.
  No migration or Salesforce metadata change. Original evidence script comes
  from Unicode letters; Arabizi numerals do not make Latin Darija Arabic-script.
  Mixed scripts (including Latin product/order references) are recorded as mixed.
- `frontend/src/chat/i18n.ts` contains five template sets (English, French,
  Arabic, Darija Latin, Darija Arabic). Historical cards retain their turn's
  language; current chat chrome follows the latest selection. Counts use
  neutral labels to avoid incorrect singular/plural constructions.
- macOS, real PostgreSQL: 268 passed, 101 paid scenarios deselected; the later
  original-wording/prompt-contract regression adds one passing test (269 total).
  Linux image: 259 passed, 9 skipped (`salesforce/` outside image), 101 deselected
  before that additional test. Frontend `test:chat`, `tsc` and build passed;
  the build reports the existing optional `metadataBase` warning.
- Real-org `salesforce verify` 41/41, `salesforce smoke` 12/12,
  `read-mcp smoke` 14/14, `write-mcp smoke` 15/15. Direct integration-user
  writes stay refused. Smoke receipts replayed the existing closed case.
- Browser: real API, PostgreSQL, both HTTP MCP servers and Salesforce, scripted
  models only. C006 browser case 00001044 reopened, then French and both Darija
  scripts updated that same case. Refresh restored language and per-run cards.
  Real evidence query confirmed exact Arabic Darija text and `ary/arabic`,
  `ary/latin`, `fr/latin` labels. Close test case as withdrawn after checks.
  Screenshot: [RTL case cards](../evidence/phase4-rtl.jpg).
- Docker's normal build timed out retrieving the Python base-image manifest.
  For local verification only, temporary Dockerfiles used the cached Phase 3
  runtime/test images with current `code/` and `tests/` copied in (dependencies
  and migrations are unchanged). `nabd-backend:phase4` services start healthy.
  A clean registry-backed rebuild remains unverified in this session.

### Darija fixture review

The paired examples in `tests/nabd/multilingual_examples.py` were reviewed for
intended meaning, script and Arabizi conventions (`3`, `7`, `9`). They distinguish
asking return eligibility from requesting money back, a delayed pot from an
unrelated issue, and a same-case follow-up from a new complaint. Both scripts
preserve the identifiers and the same GBP policies. Frontend templates were
reviewed separately; French loanwords such as dossier, commande and livraison
are intentional. This is an implementation review, not a claim of human or
native-speaker acceptance. Native-speaker usability review remains advisable
before a public release.

### Paid evaluation

The owner approved starting the 36-scenario evaluation, once each, and at most
one recheck of failures after the free code checks passed; later focused
rechecks were approved one at a time. Results follow.

### Evaluation findings and follow-up

- Initial checkpoint `e352b0a`: **21/36 automated passes**, with four additional
  manually identified failures (strict £40 boundary translations, visible tool
  argument JSON, an omitted receipt and an inaccurate delayed-status wording).
  Full synthetic messages/replies and outcomes are in
  [first-run results](../evidence/phase4-first-run.json).
- Checkpoint `94957e5`: approved recheck of those 19 failing scenarios:
  **13/19 pass, 6 fail**. See [recheck results](../evidence/phase4-recheck.json).
  The initial 17 other scenarios were not rerun with the new defaults. These
  are separate measurements, not a claim of a fresh 36/36 result.
- Remaining failures: English refund/unspecified return, Latin Darija
  return/refund/unspecified return, and Arabic Darija return. Typed assessment
  logs showed a model asking for an order without using its lookup tools.
  Latin replies still contained Arabic fragments. The regional spelling of
  enceinte also led to a legitimate clarification instead of a policy answer.
- The next implementation supplies a fresh customer-scoped order overview
  before classification and a number/product index for the Supervisor. It
  preserves model semantic matching and all authorization rules; return
  eligibility still requires the specialist's computed `get_order` check.
- Latin Darija now has a deterministic Unicode format check and at most one
  same-Supervisor rewrite with tools disabled. Invalid drafts are buffered and
  never streamed or checkpointed. A second invalid draft fails the attempt
  (superseded after the focused confirmation, below);
  retry resumes without repeating confirmed cases. This is a writing-system
  contract, not a new semantic evaluator. Other languages retain streaming.
- Evaluation clarifications: a French-dominant mixed message may label its input
  French or mixed, but must answer in the requested language/script. An ambiguous
  enceinte spelling may ask once for the order before checking its policy.
  Unspecified returns must present owned named choices; a redundant Orders call
  is unnecessary when the fresh authorized index already supplies them.
- The approved recheck is exhausted. Any focused paid confirmation of these
  final fixes needs a new explicit approval. Free verification continues first.

### Focused confirmation of `e7c876a` and follow-up fixes

- Approved focused run of the six remaining scenarios, once each
  (`english-refund`, `english-ambiguous`, `darija_latin-returns`,
  `darija_latin-refund`, `darija_latin-ambiguous`, `darija_arabic-returns`):
  **3/6 automated passes**. Both refund flows opened the Returns & Refunds case
  and cited it; Latin Darija `ambiguous` listed the four owned orders.
- Two failures were over-strict checks, not product faults:
  `english-ambiguous` asked "Which item…: Bike lights, Personalised mug, Cycling
  gloves, or Bike helmet?" (owned products, no order numbers), and
  `darija_arabic-returns` asked a named clarification from the authorized index
  (SE-1051/1052/1053) without calling Orders, which the check required before
  reaching its planned clarification step. The checks now accept owned choices
  named by product or order number, and the Arabic Darija return may ask that one
  clarification (from Orders or the index) before Orders must check eligibility.
- One genuine failure: `darija_latin-returns` raised `reply_script_invalid`
  because both the draft and its rewrite contained Arabic letters. Earlier runs
  show the pattern: whole Arabic words and even half-words (`nshرح`) inside
  Latin Darija. The rewrite had run inside the full conversation, whose system
  prompt and history contain Arabic text.
- Fix (`nabd/agents/supervisor.py`, `nabd/agents/language.py`): the one rewrite
  is now a focused call with tools disabled that receives only the draft and the
  exact list of Arabic-letter words to convert. It is accepted only if every
  standalone number of the draft (order/case numbers, amounts, dates) survives;
  otherwise the draft is kept. Any Arabic letters still left are converted to
  Arabizi in code (letters only; Latin text, numbers and identifiers unchanged)
  and a warning is logged. The turn no longer fails on the writing system alone.
  This is still a format contract, not a semantic evaluator; the code
  conversion is mechanical and can read less naturally than model Darija.
- Free suite after the fixes, macOS, real PostgreSQL: **275 passed**, 101 paid
  scenarios deselected. A focused paid confirmation of the three failed
  scenarios needs a new explicit approval.

### Recheck of `aa74bdc` — the three failed scenarios, once each

- Approved run of `english-ambiguous`, `darija_latin-returns` and
  `darija_arabic-returns`: **0/3 automated passes**. Each failure is a new,
  genuine model-behavior fault; the relaxed checks and the script repair held.
- Script: the Latin Darija reply contained no Arabic letters and the code
  Arabizi fallback was not used.
- `english-ambiguous`: the reply repeated the complaint stage's generic
  question ("tell me the product name or the order number") without naming the
  four owned orders. The prompt said both "ask a listed question" and "a generic
  clarification does not replace the order index".
- `darija_latin-returns` and the late-return step of `darija_arabic-returns`
  (after its allowed speaker clarification and a correct SE-1051 answer): one
  index order matched the named product (SE-1051, SE-1053), but the Supervisor
  asked the customer to confirm it instead of asking Orders for eligibility.
- Fix (prompt only, `prompts/supervisor.md`): a listed which-order/product
  question must name the matching index purchases; exactly one matching
  purchase is identified and goes straight to the Orders specialist. A free
  check pins these rules. Free suite: **275 passed**, 101 deselected.
- Measured in the next recheck, below.

### Second recheck of `611311a` — the same three scenarios, once each

- **2/3 automated passes.** `darija_latin-returns` and `darija_arabic-returns`
  now pass: both Darija scripts went straight from the single matching order
  to the Orders check and gave the correct allowed (SE-1051) and expired
  (SE-1053) outcomes. The Latin Darija replies stayed in Latin letters.
- `english-ambiguous` still failed: "Which item would you like to return, and
  which order is it from?", without the owned orders. The source is the
  complaint stage: it labeled an unspecified return `needs_clarification` with
  that generic question (its prompt already says eligibility questions are not
  complaints), and the Supervisor copied the listed English question verbatim.
- Fix (prompt only, `prompts/complaints.md`): a wish to return an unspecified
  purchase is `no_complaint` with no question, and any which-order/product
  question must name the matching purchases from the authorized overview.
  Free suite: **275 passed**, 101 deselected.

### Third recheck of `634ea21` — `english-ambiguous`, once

- **1/1 pass.** The complaint stage returned `no_complaint`; the reply asked
  which item and listed all four owned orders by product and number.

### Completion summary

- Every scenario that failed in the 36-scenario run has passed after its fix,
  each in its own approved run: first run 21/36, recheck 13/19, focused six
  3/6, then 0/3, 2/3 and 1/1 on the remaining failures. These are separate
  measurements of successive versions, **not** a fresh 36/36 run of the final
  version. The 17 first-run passes and the 13 recheck passes were not rerun
  after later prompt changes; the last complaint-prompt change (unspecified
  returns) was measured only on `english-ambiguous`.
- Gate evidence: all six language/forms completed core support, knowledge,
  return, refund-case and cross-language case flows with unchanged ownership,
  case identity and policy outcomes; RTL and Latin browser presentation and
  replay were verified on the real stack with scripted models.
- Final free verification: macOS, real PostgreSQL **275 passed**, 101 paid
  scenarios deselected. The org, MCP smoke, Linux image and frontend results
  above were measured earlier in Phase 4 and were not repeated for these
  prompt/agent-only changes.

### Phase 5 entry criteria

- Resume commands in [status](../status.md) pass: offline suite, `salesforce
  verify`, `read-mcp smoke`, `write-mcp smoke`.
- Carry forward the limitations above and the Phase 3 limitations. The full
  multilingual scenarios three times are the Phase 7 release measurement.

### Current limitations to carry forward

- Correct calendar dates are preserved, but some non-English model replies still
  copy English month/day names instead of following the requested ISO rendering.
  The ISO instruction is a prompt rule, not a deterministic date renderer.
- Darija fluency/orthography varies; human/native-speaker usability acceptance has
  not been claimed. Regional spelling may need one reference clarification.
- The script guard checks writing systems, not meaning. Its one focused rewrite has
  tools disabled and must keep every number; semantic translation quality still
  needs the Phase 7 evaluations. It adds one model invocation only when the first
  Latin Darija draft contains Arabic letters. Words the rewrite leaves in Arabic
  letters are transliterated mechanically, which keeps the script but may read
  awkwardly; logs show how often that happens.
- No full 36-scenario rerun of the final version, repeated release measurement,
  full unchanged Phase 2/3 paid regression, cost or latency claim is made.
- Phase 3 limitations, including staff-sync absence and the bounded case-history
  window, still apply. Native browser checks use scripted models.
