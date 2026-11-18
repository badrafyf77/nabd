# Phase 4 — Multilingual behavior

**Status: in progress (18 November 2026).** Depends on completed
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

To be completed with implementation, commands, measured results, limitations,
checkpoint and Phase 5 entry criteria.

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
one recheck of failures after the free code checks passed. Results pending.
