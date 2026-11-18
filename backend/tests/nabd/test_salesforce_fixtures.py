"""The synthetic ShopEasy dataset: legacy scenarios preserved, new ones consistent."""
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from nabd.salesforce import fixtures as fx
from nabd.salesforce import schema as s
from nabd.salesforce.soql import CUSTOMER_ID, KNOWLEDGE_KEY, ORDER_NUMBER
from shop import seed as legacy
from shop.db import ShopDB


@pytest.fixture(scope="module")
def dataset() -> fx.Dataset:
    return fx.build()


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory) -> ShopDB:
    path = tmp_path_factory.mktemp("legacy") / "shop.db"
    legacy.build_database(path, today=fx.SEED_DATE)
    return ShopDB(path)


def test_legacy_customers_and_orders_are_preserved_exactly(dataset, legacy_db):
    customers = {c.customer_id: c for c in dataset.customers}
    for row in legacy_db.query("SELECT * FROM customers"):
        customer = customers[row["id"]]
        assert (customer.first_name, customer.last_name, customer.phone, customer.email, customer.city) == (
            row["first_name"], row["last_name"], row["phone"], row["email"], row["city"])
        assert customer.language == "en"
    orders = {o.order_number: o for o in dataset.orders}
    legacy_orders = legacy_db.query("SELECT * FROM orders")
    assert len(legacy_orders) == 25
    for row in legacy_orders:
        order = orders[row["id"]]
        assert (order.customer_id, order.order_date.isoformat(), order.status, order.payment_method) == (
            row["customer_id"], row["order_date"], row["status"], row["payment"])
        assert order.total == Decimal(str(row["total"]))
        items = legacy_db.query("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (row["id"],))
        assert [(i.product, i.category, i.quantity, float(i.unit_price), int(i.returnable)) for i in order.items] == [
            (i["product"], i["category"], i["quantity"], i["unit_price"], i["returnable"]) for i in items]
        delivery = legacy_db.query_one("SELECT * FROM deliveries WHERE order_id = ?", (row["id"],))
        iso = lambda value: value.isoformat() if value else None  # noqa: E731
        d = order.delivery
        assert (d.status, d.carrier, d.tracking_number, iso(d.shipped), iso(d.expected), iso(d.original_expected),
                iso(d.delivered), d.last_location, iso(d.last_update)) == (
            delivery["status"], delivery["carrier"], delivery["tracking_number"], delivery["shipped_date"],
            delivery["expected_date"], delivery["original_date"], delivery["delivered_date"],
            delivery["last_location"], delivery["last_update"])


def test_moroccan_and_french_customers_are_added(dataset):
    languages = Counter(customer.language for customer in dataset.customers)
    assert languages == {"en": 10, "ary": 3, "fr": 2, "ar": 1}
    assert len(dataset.customers) == 16 and len(dataset.orders) == 35
    for customer in dataset.customers:
        assert CUSTOMER_ID.fullmatch(customer.customer_id)
        assert customer.email.endswith("@example.com") and customer.phone.startswith("07700900")


def test_identifiers_are_unique_and_readable_by_the_adapter(dataset):
    numbers = [order.order_number for order in dataset.orders]
    assert len(numbers) == len(set(numbers)) and all(ORDER_NUMBER.fullmatch(n) for n in numbers)
    assert len({c.customer_id for c in dataset.customers}) == len(dataset.customers)
    assert all(KNOWLEDGE_KEY.fullmatch(entry.entry_key) for entry in dataset.knowledge)
    valid = {
        "category": {v.api for v in s.PRODUCT_CATEGORIES}, "order": {v.api for v in s.ORDER_STATUSES},
        "delivery": {v.api for v in s.DELIVERY_STATUSES},
    }
    for order in dataset.orders:
        assert order.status in valid["order"] and order.delivery.status in valid["delivery"]
        assert {item.category for item in order.items} <= valid["category"]
        assert order.total == sum(item.unit_price * item.quantity for item in order.items)


def test_every_legacy_faq_and_policy_has_one_published_revision(dataset):
    published = Counter(entry.entry_key for entry in dataset.knowledge if entry.status == "Published")
    assert set(published.values()) == {1}
    assert sum(1 for key in published if key.startswith("faq.")) == len(legacy.FAQ)
    assert sum(1 for key in published if key.startswith("policy.")) == len(legacy.RETURN_POLICY)
    by_key = {entry.entry_key: entry for entry in dataset.knowledge if entry.status == "Published"}
    assert by_key["faq.delivery_cost"].body.startswith("Standard delivery costs £3.99")
    assert "help@shopeasy.example" in by_key["faq.contact_email"].body
    assert "30 days" in by_key["policy.return_window"].body
    for entry in by_key.values():
        assert " pounds" not in entry.body and " dot " not in entry.body
        assert entry.published_at is not None and entry.retired_at is None


def test_knowledge_history_contains_retired_and_draft_revisions(dataset):
    states = {(entry.revision_key, entry.status) for entry in dataset.knowledge}
    assert ("faq.delivery_cost@1", "Retired") in states
    assert ("faq.support_hours@2", "Draft") in states
    keys = [entry.revision_key for entry in dataset.knowledge]
    assert len(keys) == len(set(keys))


def test_complaints_are_consistent_with_customers_orders_and_policy(dataset):
    orders = {order.order_number: order for order in dataset.orders}
    for complaint in dataset.complaints:
        if complaint.order_number:
            assert orders[complaint.order_number].customer_id == complaint.customer_id
        assert complaint.category in s.QUEUE_FOR_CATEGORY
        assert complaint.evidence[0].kind == "customer_report"
        assert 0 <= complaint.confidence <= 1
        if complaint.needs_review:
            assert complaint.category == "General_Support" and complaint.confidence < Decimal("0.5")
        if complaint.status == "Closed":
            kinds = {item.kind for item in complaint.evidence}
            assert complaint.outcome == "resolved" and "customer_confirmation" in kinds
            assert complaint.confirmation_message_id in {item.message_id for item in complaint.evidence}
    by_issue = {complaint.issue_id: complaint for complaint in dataset.complaints}
    assert by_issue["FX-ISSUE-0004"].priority == "High"  # suspected duplicate charge
    assert {by_issue[i].priority for i in ("FX-ISSUE-0001", "FX-ISSUE-0005")} == {"Medium"}  # lost parcels
    assert {complaint.status for complaint in dataset.complaints} == {v.api for v in s.CASE_STATUSES}


def test_complaints_cover_languages_and_scripts_with_original_wording(dataset):
    reports = [item for c in dataset.complaints for item in c.evidence if item.kind != "verified_fact"]
    assert {item.language for item in reports} == {"en", "fr", "ar", "ary", "mixed"}
    assert {item.script for item in reports if item.language == "ary"} == {"latin", "arabic"}
    arabic = [item.text for item in reports if item.script == "arabic"]
    assert all(any("؀" <= char <= "ۿ" for char in text) for text in arabic)
    assert len({item.message_id for c in dataset.complaints for item in c.evidence}) == sum(
        len(c.evidence) for c in dataset.complaints)


def test_follow_ups_link_existing_complaints(dataset):
    issues = {complaint.issue_id for complaint in dataset.complaints}
    assert {item.issue_id for item in dataset.follow_ups} <= issues


def test_the_seed_date_makes_the_dataset_reproducible():
    assert fx.build() == fx.build(fx.SEED_DATE)
    later = fx.build(fx.SEED_DATE + timedelta(days=10))
    first = fx.build()
    assert later.orders[0].order_date - first.orders[0].order_date == timedelta(days=10)
    assert later.orders[0].order_number == first.orders[0].order_number
    assert fx.SEED_DATE == date(2026, 11, 18)
