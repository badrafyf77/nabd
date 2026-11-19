# Demonstrate NABD

Use [setup](README.md#fresh-checkout) to prepare the dedicated
`nabd-dev` org and browser. Salesforce keeps the records, the activity
cards show execution, and the quality report is available from an operator
shell. The [runbook](runbook.md) has start, stop and cleanup steps.

## Free scripted walkthrough

Run the scripted API and frontend in separate terminals:

```bash
cd backend
.venv-text/bin/python scripts/browser_stack.py
```

```bash
cd frontend
NABD_API_ORIGIN=http://127.0.0.1:8083 npm run dev -- --port 3001
```

Use http://localhost:3001 and sign in as **Omar Farouk (C006)**. Use a new
conversation. Stop the regular API beforehand if it shares the same
PostgreSQL database, since the scripted API needs to own the staff
subscriber.

1. Ask “How much does delivery cost?” Show the knowledge activity and the
   published delivery FAQ retrieved from Salesforce.
2. Send “My coffee grinder SE-1091 has not arrived.” Open the case card and
   use the confirmed case number to find the corresponding Salesforce case.
   The scripted complaint includes an unsupported compensation sentence in
   the staff summary. Wait for grading and check the quality report. Refresh
   Salesforce to see the corrected summary and the original evidence.
3. Send “Le même moulin à café n’est toujours pas arrivé. Mettez à jour le
   même dossier.” Show the French reply and the same case number. For an
   Arabic-script walkthrough, send “نفس مطحنة القهوة مازال ما وصلاتش، زيد هادشي
   فنفس الملف عافاك.” and show the localized activity and layout.
4. Keep the French conversation open. In Salesforce, set the case to
   **Working** and add a public Case Comment with “The team processed the
   refund of GBP 34.99. It will arrive in 3 days.” Wait for the status notice
   and then the original plus labeled scripted French translation. Reload
   the chat and confirm the same reply and case are present.
5. In Salesforce, open **NABD Support → Dashboards → NABD Dashboards →
   NABD Risk Warnings**. The C004 fixture should have an active
   persistent-negative-sentiment episode with two customer contacts and one
   staff follow-up task. Open the warning and explain its evidence.
6. From `backend/` run `PYTHONPATH=code .venv-text/bin/python -m nabd quality report`.
   Use `quality show RUN_UUID` from the operator shell if the audience is
   authorized to see its restricted trace. It shows the grade and one
   summary revision receipt.

The free demo scripts the models. It uses live PostgreSQL, read/write MCP,
Salesforce and Pub/Sub. The initial case, public reply and status edits are
test records. Use the runbook's cleanup afterwards.

To measure event delivery, open the dev page with `?nabdLatency=1`, or set
`window.nabdLatency = []` in the browser console. The array holds event IDs,
types and timestamps, and caps at 2,000. Submit enough scripted turns to see
at least 200 live events and subtract each event's `createdAt` from
`receivedAt`. Compare the browser host clock with PostgreSQL
`clock_timestamp()` using the midpoint of several database requests and
record the lowest-RTT sample. For staff latency, leave the chat idle and
alternate between Working and Waiting on Staff 20 times. Pair each pre-write
wall-clock timestamp with the matching browser event ID and timestamp. Record
timeouts and the maximum as well as p95.

## Real-model walkthrough

Start Compose and run the frontend on port 3000. Use the same chat inputs
with an authorized OpenAI key. Choose the test case and staff reply as above.
Wording and case outcomes are live model behavior. A faithful staff summary
can pass review without a correction. Open the quality report after the turn
and verify that the background grade appears without altering the chat reply.
This version incurs model charges, including evaluations and translations.
