# NABD release follow-up backlog

- **Measure staff sync under concurrent writes.** The isolated browser run
  delivered 19 of 20 staff changes within five seconds. One took 6.597 s
  before reaching the browser, and only about 22 ms elapsed from its stored SSE
  event to browser receipt. Capture Salesforce REST duration, Pub/Sub arrival,
  and projection time together before adjusting polling or retry behavior.
- **Automate trace expiry and restrict database roles.** The operator CLI reads
  via the configured DB role and traces contain customer-scoped tool data.
  Add a scoped operator role and retention job that skips pending reviews and
  preserves the summary-revision idempotency key.
- **Improve cost reporting for retries and mixed model pricing.** A resumed
  run records only the finishing attempt. Add retained aggregate usage for
  interrupted attempts if accounting requires billed totals. Apply each
  configured model's schedule when different models are used.
- **Expand warning validation with staff.** The baseline review covers 45
  fixture customer/rule combinations. Validate labels after staff edits,
  reopened cases and additional time windows before estimating error rates in
  a wider population.
- **Review multilingual coverage and evaluator calibration.** The initial
  paid run missed five explicit foreign-order lookups, mislabeled one
  standard Arabic turn and changed one strict French delivery-price boundary.
  The evaluator missed the price issue. The Darija read-through found
  awkward wording and one incorrect weekday. The focused recheck recovered
  the price boundary and two foreign-order lookups. Keep the holdout frozen
  in future campaign runs and use independent examples for later tuning.
  [Campaign evidence](evidence/phase7-campaign.json).
- **Capture earlier tool evidence after crash.** A partial trace marks the
  finishing attempt's evidence boundary. Evaluate keeping short-lived earlier
  attempt tool results if operators routinely need to diagnose partial reviews.
