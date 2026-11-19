"""Seed repeatability and staff-edit preservation, against an in-memory org."""
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nabd.salesforce import fixtures as fx
from nabd.salesforce import seed
from nabd.salesforce import schema as s
from salesforce_fakes import FakeSalesforce

STAFF = "005000000000000001"


def _run(org: FakeSalesforce, dataset: fx.Dataset, *, reset: bool = False) -> seed.SeedReport:
    return asyncio.run(seed.Seeder(org, staff_user_id=STAFF).run(seed.records(dataset), reset=reset))


def _totals(report: seed.SeedReport) -> Counter:
    total = Counter()
    for counts in report.counts.values():
        total.update(counts)
    return total


@pytest.mark.parametrize(("field_type", "fixture", "salesforce"), [
    ("Number", Decimal("129.99"), 129.99),
    ("Number", 1, 1.0),
    ("Number", Decimal("0.940"), 0.94),
    ("Date", date(2026, 11, 18), "2026-11-18"),
    ("DateTime", datetime(2026, 11, 16, 14, 5, tzinfo=timezone.utc), "2026-11-16T14:05:00.000+0000"),
    ("Checkbox", False, False),
    ("Text", None, None),
    ("Text", "", None),
])
def test_fixture_and_salesforce_values_compare_equal(field_type, fixture, salesforce):
    assert seed.canonical(field_type, fixture) == seed.canonical(field_type, salesforce)


def test_records_are_ordered_parents_first_and_use_contract_fields():
    records = seed.records(fx.build())
    order = list(dict.fromkeys(record.sobject for record in records))
    assert order.index("Account") < order.index("Contact") < order.index(s.ORDER.name)
    assert order.index(s.ORDER.name) < order.index(s.ORDER_ITEM.name)
    assert order.index("Case") < order.index(s.EVIDENCE.name) < order.index("Task")
    keys = Counter((record.sobject, record.key) for record in records)
    assert max(keys.values()) == 1


def test_first_seed_creates_everything_and_second_seed_writes_nothing():
    org, dataset = FakeSalesforce(), fx.build()
    first = _run(org, dataset)
    assert set(_totals(first)) == {"create"}
    assert _totals(first)["create"] == len(seed.records(dataset))
    writes = len(org.writes)
    second = _run(org, dataset)
    assert set(_totals(second)) == {"unchanged"} and second.writes == 0
    assert len(org.writes) == writes
    case = org.find("Case", "FX-ISSUE-0004")
    assert case["OwnerId"] == org.queues["NABD_Billing_Payments"]
    evidence = org.find(s.EVIDENCE.name, "FX-ISSUE-0004:FX-MSG-0004")
    assert evidence["Case__c"] == case["Id"]
    assert evidence["Original_Text__c"].startswith("Bonjour, j'ai été débité deux fois")


def test_staff_edits_are_preserved_on_reseed():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    org.find("Case", "FX-ISSUE-0002")["Status"] = "Working"  # staff moved the case on
    org.find("Case", "FX-ISSUE-0003")["OwnerId"] = "005000000000000099"  # staff took ownership
    report = _run(org, dataset)
    assert report.preserved == ["Case:FX-ISSUE-0002", "Case:FX-ISSUE-0003"]
    assert report.writes == 0
    assert org.find("Case", "FX-ISSUE-0002")["Status"] == "Working"


def test_changed_fixtures_update_only_unedited_records():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    org.find(s.DELIVERY.name, "SE-1042")["Last_Location__c"] = "Edited by staff"
    changed = replace(dataset, orders=tuple(
        replace(order, delivery=replace(order.delivery, last_location="Moved"))
        if order.order_number in ("SE-1042", "SE-1051") else order
        for order in dataset.orders))
    report = _run(org, changed)
    assert report.counts[s.DELIVERY.name]["update"] == 1
    assert report.preserved == [f"{s.DELIVERY.name}:SE-1042"]
    assert org.find(s.DELIVERY.name, "SE-1051")["Last_Location__c"] == "Moved"
    assert org.find(s.DELIVERY.name, "SE-1042")["Last_Location__c"] == "Edited by staff"
    # The update did not resend the non-reparentable master-detail parent (the fake asserts this).


def test_records_without_a_seed_fingerprint_are_never_overwritten():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    contact = org.find("Contact", "C003")
    contact[s.SEED_FINGERPRINT] = None
    report = _run(org, dataset)
    assert "Contact:C003" in report.preserved


def test_reset_is_explicit_and_restores_fixture_values():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    org.find("Case", "FX-ISSUE-0002")["Status"] = "Working"
    report = _run(org, dataset, reset=True)
    assert report.counts["Case"]["restore"] == 1 and report.preserved == []
    assert org.find("Case", "FX-ISSUE-0002")["Status"] == "Waiting on Customer"
    assert _run(org, dataset).writes == 0


def test_tasks_are_matched_by_key_without_duplicates():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    _run(org, dataset)
    tasks = org.records["Task"].values()
    assert sorted(task["NABD_Task_Key__c"] for task in tasks) == ["FX-TASK-0001", "FX-TASK-0002", "FX-TASK-0003"]
    assert {task["OwnerId"] for task in tasks} == {STAFF}
    case_ids = {org.find("Case", issue)["Id"] for issue in ("FX-ISSUE-0001", "FX-ISSUE-0003", "FX-ISSUE-0006")}
    assert {task["WhatId"] for task in tasks} == case_ids


def test_missing_queues_stop_the_seed_before_any_write():
    org = FakeSalesforce()
    org.queues.pop("NABD_Delivery")
    with pytest.raises(RuntimeError, match="NABD_Delivery"):
        _run(org, fx.build())
    assert org.writes == []


def test_records_seeded_before_a_field_was_added_are_not_mistaken_for_staff_edits():
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    # Recreate the Phase 1 state: no Policy_Days__c and fingerprints computed without it.
    for record in seed.records(dataset):
        if record.sobject != s.KNOWLEDGE.name:
            continue
        row = org.find(s.KNOWLEDGE.name, record.key)
        row["Policy_Days__c"] = None
        old = {k: v for k, v in seed.canonical_values(record).items() if k != "Policy_Days__c"}
        row[s.SEED_FINGERPRINT] = seed.fingerprint(old)
    org.find(s.KNOWLEDGE.name, "policy.refunds@1")["Policy_Days__c"] = 14.0  # staff set a value
    report = _run(org, dataset)
    assert report.preserved == [f"{s.KNOWLEDGE.name}:policy.refunds@1"]
    # Unedited revisions are rewritten once with the new fingerprint; only the return window gains a value.
    assert report.counts[s.KNOWLEDGE.name]["update"] == len(dataset.knowledge) - 1
    assert org.find(s.KNOWLEDGE.name, "policy.return_window@1")["Policy_Days__c"] == 30.0
    assert _run(org, dataset).writes == 0


def test_fields_added_in_two_later_phases_are_not_mistaken_for_staff_edits():
    # Phase 3 fingerprints include the Phase 3 case fields but not the Phase 6 streak.
    org, dataset = FakeSalesforce(), fx.build()
    _run(org, dataset)
    cases = [record for record in seed.records(dataset) if record.sobject == "Case"]
    for record in cases:
        row = org.find("Case", record.key)
        row["Negative_Streak__c"] = None
        old = {k: v for k, v in seed.canonical_values(record).items() if k != "Negative_Streak__c"}
        row[s.SEED_FINGERPRINT] = seed.fingerprint(old)
    edited = org.find("Case", "FX-ISSUE-0002")
    edited["Priority"] = "High"  # a staff edit to a Phase 1 field is still preserved
    report = _run(org, dataset)
    assert report.preserved == ["Case:FX-ISSUE-0002"]
    assert report.counts["Case"]["update"] == len(cases) - 1
    assert org.find("Case", "FX-ISSUE-0001")["Negative_Streak__c"] == 2
    assert _run(org, dataset).writes == 0


def test_fixture_contacts_carry_sentiment_and_streaks():
    dataset = fx.build()
    complaints = {complaint.issue_id: complaint for complaint in dataset.complaints}
    streaks = {key: seed.negative_streak(complaint) for key, complaint in complaints.items()}
    # FX-ISSUE-0001 is negative at two consecutive contacts; the resolved case ends positive.
    assert streaks["FX-ISSUE-0001"] == 2 and streaks["FX-ISSUE-0005"] == 0 and streaks["FX-ISSUE-0006"] == 0
    assert {key for key, value in streaks.items() if value >= 2} == {"FX-ISSUE-0001"}
    for complaint in dataset.complaints:
        for item in complaint.evidence:
            sentiment = seed.assessed_sentiment(complaint, item)
            assert (sentiment is None) == (item.kind == "verified_fact"), item.message_id
