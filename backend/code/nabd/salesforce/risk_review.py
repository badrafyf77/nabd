"""Compare active Salesforce warnings with independently labeled seed examples."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from nabd.salesforce import fixtures
from nabd.salesforce.rest import SalesforceClient
from nabd.salesforce.soql import literal

# Review labels for the baseline seed. All other fixture customer/rule pairs
# are negative. C008 is reserved for smokes and excluded from the population.
LABELS = {
    ("C004", "persistent_negative_sentiment"): "The open lost-watch complaint has a second negative contact.",
}


async def run(client: SalesforceClient) -> int:
    customers = sorted(c.customer_id for c in fixtures.build().customers if c.customer_id != "C008")
    allowed = ", ".join(map(literal, customers))
    rows = await client.query(
        "SELECT Account__r.ShopEasy_Customer_Id__c, Rule_Code__c FROM Risk_Episode__c "
        f"WHERE Active_Key__c != null AND Account__r.ShopEasy_Customer_Id__c IN ({allowed})"
    )
    observed = {(row["Account__r"]["ShopEasy_Customer_Id__c"], row["Rule_Code__c"]) for row in rows}
    # Fixture cases are created at seed time. Their evidence capture date is
    # not the moment the case started aging in Salesforce.
    aged_case = await client.query(
        "SELECT CreatedDate FROM Case WHERE NABD_Issue_Id__c = 'FX-ISSUE-0004' LIMIT 1"
    )
    if not aged_case:
        print("Missing baseline case FX-ISSUE-0004; seed the fixture before reviewing warnings.")
        return 1
    eligible_at = datetime.fromisoformat(aged_case[0]["CreatedDate"]) + timedelta(hours=24)
    labels = dict(LABELS)
    if datetime.now(timezone.utc) >= eligible_at:
        labels["C013", "high_priority_unresolved"] = "The baseline high-priority duplicate charge is at least 24 hours old."
    expected = set(labels)
    population = {(customer, rule.code) for customer in customers for rule in fixtures.RISK_RULES}
    negatives = len(population - expected)
    fp = observed - expected
    fn = expected - observed
    print(json.dumps({
        "fixture_customers": len(customers), "labeled_pairs": len(population),
        "expected_warnings": [{"customer": customer, "rule": rule, "basis": basis}
                              for (customer, rule), basis in sorted(labels.items())],
        "C013_high_priority_eligible_at": eligible_at.isoformat(),
        "true_positives": sorted(observed & expected), "false_positives": sorted(fp),
        "false_negatives": sorted(fn), "true_negatives": len(population - (expected | observed)),
        "false_positive_rate": len(fp & population) / negatives,
        "unlabeled_rules": sorted(observed - population),
    }, indent=2))
    return int(bool(fp or fn))
