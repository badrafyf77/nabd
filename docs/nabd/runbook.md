# NABD operations runbook

This runbook covers the ShopEasy demonstration stack and the dedicated
Salesforce Developer Edition org. Use [setup](README.md#fresh-checkout) for
a new checkout and [demo](demo.md) for the scripted walkthrough.

## Start, stop and inspect

From `backend/`:

```bash
docker compose -f docker-compose.nabd.yml up -d --build --wait api
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
curl --fail http://127.0.0.1:8091/healthz
curl --fail http://127.0.0.1:8092/healthz
docker compose -f docker-compose.nabd.yml ps -a
docker compose -f docker-compose.nabd.yml logs --tail 60 api read-mcp write-mcp migrate
```

Health reports that a process responds. API readiness checks the PostgreSQL
application and LangGraph migration revisions. Sign in as a fixture
customer to verify the end-to-end configuration. The web app proxies
`/api` on the same origin. From `frontend/`, use `npm run dev` and open
http://localhost:3000.

To stop while preserving data, use
`docker compose -f docker-compose.nabd.yml --profile test down`. Leave out
volume removal. The production-shaped local stack runs real models when
customers send messages or background jobs already exist. Use the scripted
stack for verification without model calls.

## Migrations and Salesforce deploy

Back up PostgreSQL before upgrades. Stop the active API for a manual schema
upgrade. The normal Compose startup runs the explicit migration service and
then starts the API. For a host process, use the isolated text environment:

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd migrate
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce check
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce deploy --target-org nabd-dev
PYTHONPATH=code .venv-text/bin/python -m nabd salesforce verify --target-org nabd-dev
```

The deploy wrapper pauses the hourly risk schedule during Apex deployment and
restores it afterwards. If a process was forcibly interrupted, confirm the
schedule in Salesforce and use `salesforce configure --target-org nabd-dev` to
restore it if needed. Reapply integration access through `configure` after a
client app policy change. Regenerate metadata from the schema contract
before deploying schema changes. Use `seed` for repeatable fixture creation.
`reset` overwrites fixture edits and requires an explicit target confirmation.

Forward recovery is the normal path. Migration `0005_quality` refuses to
downgrade while its tables contain data. To pause grading during an incident,
restart the API with `NABD_QUALITY_ENABLED=false`. For a wording-only pause,
use `NABD_QUALITY_SUMMARY_CORRECTION_ENABLED=false`. Disabled grading leaves
new turns without a review job and continues collecting traces. A disabled
correction worker leaves existing jobs queued and skips creating new ones.

## Background jobs

| Job | Durable state and recovery |
| --- | --- |
| Chat runs | `nabd.runs` and LangGraph checkpoints. An expired lease permits another attempt. Messages/events and case receipts preserve stable identities. |
| Case command reconciliation | `nabd.case_commands`. Lookup the original operation receipt when a response is unclear. Reuse the same command and ID on retries. |
| Staff sync and translation | `nabd.sync_cursors`, `case_syncs`, `staff_replies`. One subscriber per database/topic; replay after disconnect, re-read current authorized state, reconcile periodically or on snapshot/reconnect. |
| Quality review | `nabd.quality_reviews`. Claims are fenced by a lease token. Malformed grading retries to a capped terminal failure. A model call can repeat after lease expiry. One grade can commit. |
| Summary correction | `nabd.summary_revisions`. The source command is unique and the correction is committed to PostgreSQL before dispatch. Recover a dispatched operation from its receipt. Version conflicts and staff overrides are terminal. |
| Risk warnings | Salesforce risk events plus the hourly sweep. Staff can check freshness and the task linked to each episode in NABD Dashboards. |

Quality status and model usage are accessible from the operator shell:

```bash
PYTHONPATH=code .venv-text/bin/python -m nabd quality report --since 2026-11-19
PYTHONPATH=code .venv-text/bin/python -m nabd quality show RUN_UUID
```

The aggregate reports use the stored event timestamps and recorded Responses
API usages. Reasoning tokens are a subset of output and cached tokens are a
subset of input. USD estimates use the configured per-million-token inputs.
For an estimate across models with different prices, calculate those models
separately. Resumed turns can omit tokens billed to earlier attempts. Failed
or timed-out model requests without a usage payload can also incur charges.

## Incidents

| Symptom | Check and recover |
| --- | --- |
| PostgreSQL down or API `not_ready` | Check `db` health, free disk, credentials and `migrate` logs. Bring it back before restarting API workers. The API cannot resume a run before PostgreSQL is available. |
| Chat `chat_not_configured` | Confirm the API can load `backend/.env.nabd` or equivalent service env, including both unique context secrets, OpenAI key and Salesforce integration configuration. Confirm per-service host settings for Compose. |
| Salesforce reads fail | Run `salesforce smoke` and `read-mcp smoke`. Check My Domain, client-app approval and integration profile. Reauthenticate the separate `nabd-dev` CLI session for admin commands if expired. |
| Unclear case/revision command | Find the operation ID in its private queue. Inspect the existing receipt. If identity or version differs, investigate the source command and current Salesforce state. Never generate a fresh operation ID to force a retry. |
| OpenAI timeout or quota error | Check service credentials and provider quota. Chat retries preserve existing case receipts. Quality work retries independently. Disable quality temporarily to preserve quota for interactive turns. |
| Staff sync lag | Check API logs for Pub/Sub reconnects and confirm integration subscribe access. Reload the chat to request reconciliation. For the dedicated smoke, stop the ordinary API subscriber first or use a separate database. |
| Warning/dashboard mismatch | Check rule freshness, hourly job state and `salesforce analytics-reconcile`. `risk-review` compares the fixture labels with the current org; first account for staff edits and test cases. |

## Secrets, test-data cleanup and retention

Store the database URL, OpenAI key, client credentials and MCP signing secrets
in `backend/.env.nabd` or a secret manager. Leave that file out of Git.
Rotate the OpenAI key and Salesforce client-app secret at their source, update
the service environment, restart its consumers, then run the relevant no-model
smokes. Rotate each signing secret on the API and its corresponding MCP
server together and allow in-flight calls to settle before restarting.

For browser verification, close only the case created by the walkthrough
with the duplicate outcome and remove its temporary Case Comment. Keep
receipts for idempotency and audit. Dedicated smoke commands identify their
fixture cases and close them themselves.

Grant `quality show` and PostgreSQL access only to operators authorized to see
customer records. Diagnostic traces contain tool data and grades can contain
verbatim quotes. Restrict backups, terminal transcripts and campaign JSONL
accordingly. Use a 30-day diagnostic-trace retention policy for the demo and
make operators responsible for implementing it in storage. Before pruning,
check that candidate runs have no pending review, agree the cutoff and back up
only if appropriate for the retention policy. Keep case command and revision
identities while the corresponding writes or receipts may still be replayed.
Automated trace expiry and distinct DB roles for operators are follow-ups.
