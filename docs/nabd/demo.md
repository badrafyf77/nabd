# Demonstrate NABD

Use [setup](README.md#fresh-checkout) to prepare the dedicated
`nabd-dev` org and browser. Salesforce keeps the records, the activity
cards show execution, and the quality report is available from an operator
shell. The [runbook](runbook.md) has start, stop and cleanup steps.

## Four-minute live demo

Follow Amina's delayed delivery from chat to Salesforce and back. Start the
[recording stack](#free-scripted-walkthrough) before the clock. In the chat,
sign in as **Amina Haddad (C003)** and start a new conversation. Open the
`nabd-dev` Salesforce **Cases** list in a second tab and keep the
[quality report](#show-the-summary-correction) command ready. On a fresh
recording-mode launch, the first complaint creates a separate case for
order `SE-1061`.

| Time | Do this | Say this |
| --- | --- | --- |
| 0:00–0:20 | Show the chat. | “NABD gives ShopEasy customers support in multiple languages and turns complaints into cases staff can handle in Salesforce. This walkthrough uses scripted model responses with a live Salesforce sandbox.” |
| 0:20–0:50 | Send prompt 1 and expand the activity card for the knowledge lookup. | “The answer comes from ShopEasy's published delivery FAQ. The activity shows what NABD looked up.” |
| 0:50–1:40 | Send prompt 2. Show the order activity and copy the confirmed case number. Find the same case in Salesforce. | “NABD checks the order and opens a Delivery case. Staff get the complaint with its supporting evidence.” |
| 1:40–2:15 | Send prompt 3 in the same chat. Point to the French reply and the case number. | “Amina switched to French and the follow-up went onto the same case. Staff keep the full context in one place.” |
| 2:15–3:15 | On that Salesforce case, add the public comment below. Switch back to the chat and show the original reply and labeled French translation. | “A staff member replied from Salesforce. Amina sees that update in the chat with the original and its translation.” |
| 3:15–4:00 | Show the aggregate quality report and point to the confirmed summary correction. | “A background review corrected an unsupported claim in the staff summary. The customer gets continuity in their language and the team works from one case.” |

**Prompt 1**

```text
How much does delivery cost?
```

**Prompt 2**

```text
My cast iron cooking pot from order SE-1061 still hasn't arrived.
```

**Prompt 3**

```text
La même cocotte n’est toujours pas arrivée. Ajoutez cette information au même dossier.
```

**Public Salesforce case comment**

```text
We asked the carrier to locate your parcel. We will update you here.
```

The comment represents a staff update in the sandbox. Use the exact text so
it matches the scripted French translation.

With **three minutes**, skip the FAQ and close after the translated reply.
With **five minutes**, reload the chat to show the reply persists, then open
**NABD Risk Warnings** in Salesforce and point to the C004 fixture's warning
and follow-up task. If the staff reply is delayed, move to the quality report
and return to the chat before closing. After presenting, close the sandbox
case and remove the temporary comment using the [runbook](runbook.md#secrets-test-data-cleanup-and-retention).

### Show the summary correction

After the first complaint creates the Salesforce case, run this from
`backend/`. The database URL must match the recording stack.

```bash
NABD_DATABASE_URL=postgresql://nabd:nabd_dev@127.0.0.1:55432/nabd_recording \
  PYTHONPATH=code .venv-text/bin/python -m nabd quality report |
  jq '{completed_turns, review_jobs, summary_corrections}'
```

Look for `"summary_corrections": {"confirmed": 1}` on a fresh recording
database. That count means Salesforce accepted the wording-only correction.
If the correction is still absent or pending, give the background job a
moment and rerun the command. Send the French follow-up after the correction
is confirmed so the review can finish on the original case revision.

Capture this clip before sending the French reply and place it at the end
of the video. Point to `confirmed`, then switch to the same Salesforce case
and show the refreshed **English Summary**. The scripted correction reads
“The customer reports that their cast iron cooking pot has not arrived.”
Say, “The background quality review removed an unsupported compensation
claim from the staff summary. The original customer complaint is still
attached as evidence.”

### Recording checklist

- Record the browser with the chat and Salesforce tabs ready. Start the
  recording with Amina signed in and the new conversation open. Keep the
  case number visible each time you switch between chat and Salesforce.
- Hold on the French reply and translated staff comment long enough to read
  them. Trim waiting time between the staff post and the chat update.
- Capture the quality report as a separate short clip showing the aggregate
  output. Keep credentials and restricted traces outside the recording.

## Free scripted walkthrough

Use the dedicated local `nabd_recording` database so the scripted API can run
alongside the regular backend and hold its own staff subscriber lease. Run
the API from the repository root in one terminal.

```bash
cd backend
docker compose -f docker-compose.nabd.yml up -d --wait db
# Create the demo database once, if it does not already exist.
docker compose -f docker-compose.nabd.yml exec -T db createdb -U nabd nabd_recording
export NABD_DATABASE_URL=postgresql://nabd:nabd_dev@127.0.0.1:55432/nabd_recording
.venv-text/bin/python scripts/browser_stack.py --recording
```

Start the frontend from the repository root in a second terminal.

```bash
cd frontend
NABD_API_ORIGIN=http://127.0.0.1:8083 npm run dev -- --port 3001 --hostname 127.0.0.1
```

Open [the scripted chat](http://127.0.0.1:3001), sign in as **Amina Haddad
(C003)**, and start a new conversation. Recording mode prints the Salesforce
case subject on startup. After a rehearsal that opened a case, restart the
scripted API and start another chat for a clean take. Each launch uses a
fresh subject and the following turns update the case opened in that take.

1. Ask “How much does delivery cost?” Show the knowledge activity and the
   published delivery FAQ retrieved from Salesforce.
2. Send “My cast iron cooking pot from order SE-1061 still hasn't arrived.”
   Open the case card and use its number to find the corresponding Salesforce case.
   The scripted complaint includes an unsupported compensation sentence in
   the staff summary. Check for a confirmed correction before sending the
   French follow-up. Refresh Salesforce to see the revised summary and
   original evidence. [Quality report](#show-the-summary-correction).
3. Send “La même cocotte n’est toujours pas arrivée. Ajoutez cette information
   au même dossier.” Show the French reply and the same case number. For an
   Arabic-script walkthrough, send “نفس طنجرة الحديد مازال ما وصلاتش، زيد هادشي
   فنفس الملف عافاك.” and show the localized activity and layout.
4. Keep the French conversation open. In Salesforce, set the case to
   **Working** and add a public Case Comment with the [recording text](#four-minute-live-demo).
   Wait for the status notice and then the original plus labeled scripted
   French translation. Reload
   the chat and confirm the same reply and case are present.
5. In Salesforce, open **NABD Support → Dashboards → NABD Dashboards →
   NABD Risk Warnings**. The C004 fixture should have an active
   persistent-negative-sentiment episode with two customer contacts and one
   staff follow-up task. Open the warning and explain its evidence.
6. Show the [aggregate quality report](#show-the-summary-correction)
   with the confirmed summary correction. Use `quality show RUN_UUID`
   with the recording database from the operator shell if the audience is
   authorized to see the restricted trace, grade, and summary revision receipt.

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
