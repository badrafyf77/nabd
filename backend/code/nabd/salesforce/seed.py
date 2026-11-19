"""Repeatable fixture upserts that preserve staff edits.

Each seeded record stores ``NABD_Seed_Fingerprint__c``: a hash of the values
the seed last wrote. On the next run:

* missing record                       -> create it
* values still equal the fingerprint   -> unchanged, or update if the fixture changed
* values differ from the fingerprint   -> someone edited it in Salesforce: preserve
* no fingerprint (not created by seed) -> preserve

``reset`` is a separate, explicit mode that restores fixture values even over
staff edits. Neither mode deletes records or touches non-fixture data.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from nabd.salesforce import fixtures as fx
from nabd.salesforce import schema as s
from nabd.salesforce.rest import SalesforceClient
from nabd.salesforce.soql import literal_list

Action = Literal["create", "update", "unchanged", "preserve", "restore"]
STAFF_OWNER = "__staff__"  # placeholder for the user running the seed


@dataclass(frozen=True)
class Reference:
    target: str  # sObject, or "Group"/"User" for owners
    key: str


@dataclass(frozen=True)
class SeedRecord:
    sobject: str
    key: str
    values: dict[str, Any]
    references: dict[str, Reference] = field(default_factory=dict)

    @property
    def obj(self) -> s.SObject:
        return s.sobject(self.sobject)


# --- Fixture -> Salesforce records -------------------------------------------------


def _account(customer: fx.Customer) -> SeedRecord:
    return SeedRecord("Account", customer.customer_id, {
        "Name": f"{customer.first_name} {customer.last_name}", "Phone": customer.phone,
        "BillingCity": customer.city, "BillingCountry": "United Kingdom",
        "Preferred_Language__c": customer.language,
    })


def _contact(customer: fx.Customer) -> SeedRecord:
    return SeedRecord("Contact", customer.customer_id, {
        "FirstName": customer.first_name, "LastName": customer.last_name, "Email": customer.email,
        "Phone": customer.phone, "MailingCity": customer.city,
    }, {"AccountId": Reference("Account", customer.customer_id)})


def _order_records(order: fx.Order) -> list[SeedRecord]:
    delivery = order.delivery
    records = [SeedRecord(s.ORDER.name, order.order_number, {
        "Name": order.order_number, "Order_Date__c": order.order_date, "Status__c": order.status,
        "Total_GBP__c": order.total, "Payment_Method__c": order.payment_method,
    }, {"Account__c": Reference("Account", order.customer_id),
        "Contact__c": Reference("Contact", order.customer_id)})]
    for item in order.items:
        records.append(SeedRecord(s.ORDER_ITEM.name, f"{order.order_number}-{item.line_number}", {
            "Name": item.product, "Line_Number__c": item.line_number, "Category__c": item.category,
            "Quantity__c": item.quantity, "Unit_Price_GBP__c": item.unit_price, "Returnable__c": item.returnable,
        }, {"Order__c": Reference(s.ORDER.name, order.order_number)}))
    records.append(SeedRecord(s.DELIVERY.name, order.order_number, {
        "Name": f"{order.order_number} delivery", "Status__c": delivery.status, "Carrier__c": delivery.carrier,
        "Tracking_Number__c": delivery.tracking_number, "Shipped_Date__c": delivery.shipped,
        "Expected_Date__c": delivery.expected, "Original_Expected_Date__c": delivery.original_expected,
        "Delivered_Date__c": delivery.delivered, "Last_Location__c": delivery.last_location,
        "Last_Update_Date__c": delivery.last_update,
    }, {"Order__c": Reference(s.ORDER.name, order.order_number)}))
    return records


def _knowledge(entry: fx.Knowledge) -> SeedRecord:
    return SeedRecord(s.KNOWLEDGE.name, entry.revision_key, {
        "Name": entry.title, "Entry_Key__c": entry.entry_key, "Revision__c": entry.revision,
        "Entry_Type__c": entry.entry_type, "Status__c": entry.status, "Language__c": "en",
        "Topic__c": entry.topic, "Question__c": entry.question, "Body__c": entry.body,
        "Search_Keywords__c": entry.keywords, "Published_At__c": entry.published_at,
        "Retired_At__c": entry.retired_at, "Policy_Days__c": entry.policy_days,
    })


def _case(complaint: fx.Complaint) -> SeedRecord:
    references = {
        "AccountId": Reference("Account", complaint.customer_id),
        "ContactId": Reference("Contact", complaint.customer_id),
        "OwnerId": Reference("Group", s.QUEUE_FOR_CATEGORY[complaint.category].developer_name),
    }
    if complaint.order_number:
        references["ShopEasy_Order__c"] = Reference(s.ORDER.name, complaint.order_number)
    return SeedRecord("Case", complaint.issue_id, {
        "Subject": complaint.subject, "Description": complaint.evidence[0].text, "Status": complaint.status,
        "Priority": complaint.priority, "Origin": "NABD Chat", "Complaint_Category__c": complaint.category,
        "Customer_Language__c": complaint.language, "Sentiment__c": complaint.sentiment,
        "Urgency__c": complaint.urgency, "Classification_Confidence__c": complaint.confidence,
        "Needs_Review__c": complaint.needs_review, "English_Summary__c": complaint.summary,
        "Resolution_Outcome__c": complaint.outcome,
        "Resolution_Confirmed_At__c": complaint.confirmed_at,
        "Resolution_Confirmation_Message__c": complaint.confirmation_message_id,
        "NABD_Conversation_Id__c": complaint.conversation_id,
        "NABD_Assessed_Priority__c": complaint.priority, "NABD_Priority_Reason__c": complaint.priority_reason,
        "NABD_Summary_Hash__c": summary_hash(complaint.summary),
        "Staff_Action_Required__c": complaint.staff_action,
        "Staff_Action_Completed__c": complaint.staff_action_completed,
        "Negative_Streak__c": negative_streak(complaint),
    }, references)


def assessed_sentiment(complaint: fx.Complaint, evidence: fx.Evidence) -> str | None:
    """NABD's sentiment at a customer contact; verified facts carry none."""
    if evidence.kind not in s.CONTACT_EVIDENCE_KINDS:
        return None
    return evidence.sentiment or complaint.sentiment


def negative_streak(complaint: fx.Complaint) -> int:
    """The gateway's streak for fixture history: each fixture message is one contact."""
    streak = 0
    contacts = sorted((item for item in complaint.evidence if item.kind in s.CONTACT_EVIDENCE_KINDS),
                      key=lambda item: item.captured_at)
    for item in contacts:
        streak = streak + 1 if assessed_sentiment(complaint, item) in s.NEGATIVE_SENTIMENTS else 0
    return streak


def summary_hash(text: str) -> str:
    """The hash the case gateway compares to tell NABD's summary from a staff edit."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _evidence(complaint: fx.Complaint, evidence: fx.Evidence) -> SeedRecord:
    return SeedRecord(s.EVIDENCE.name, f"{complaint.issue_id}:{evidence.message_id}", {
        "Source_Message_Id__c": evidence.message_id, "Evidence_Kind__c": evidence.kind,
        "Original_Text__c": evidence.text, "Original_Language__c": evidence.language,
        "Original_Script__c": evidence.script, "Verified_Source__c": evidence.verified_source,
        "Captured_At__c": evidence.captured_at, "NABD_Conversation_Id__c": complaint.conversation_id,
        "Assessed_Sentiment__c": assessed_sentiment(complaint, evidence),
    }, {"Case__c": Reference("Case", complaint.issue_id)})


def _task(follow_up: fx.FollowUp, complaint: fx.Complaint) -> SeedRecord:
    return SeedRecord("Task", follow_up.task_key, {
        "Subject": follow_up.subject, "Description": follow_up.description, "Status": "Not Started",
        "Priority": "Normal", "ActivityDate": follow_up.due, "NABD_Follow_Up_Type__c": "case_follow_up",
    }, {"WhatId": Reference("Case", complaint.issue_id), "WhoId": Reference("Contact", complaint.customer_id),
        "OwnerId": Reference("User", STAFF_OWNER)})


def _risk_rule(rule: fx.RiskRule) -> SeedRecord:
    return SeedRecord(s.RISK_RULE.name, rule.rule_key, {
        "Name": rule.name, "Rule_Code__c": rule.code, "Version__c": rule.version, "Active__c": True,
        "Description__c": rule.description, "Parameters_JSON__c": rule.parameters,
    })


def records(dataset: fx.Dataset) -> list[SeedRecord]:
    """All fixture records, parents before children."""
    complaints = {complaint.issue_id: complaint for complaint in dataset.complaints}
    result = [_account(item) for item in dataset.customers]
    result += [_contact(item) for item in dataset.customers]
    order_records = [record for order in dataset.orders for record in _order_records(order)]
    for sobject in (s.ORDER.name, s.ORDER_ITEM.name, s.DELIVERY.name):
        result += [record for record in order_records if record.sobject == sobject]
    result += [_knowledge(item) for item in dataset.knowledge]
    result += [_risk_rule(item) for item in dataset.risk_rules]
    result += [_case(item) for item in dataset.complaints]
    result += [_evidence(complaint, evidence) for complaint in dataset.complaints for evidence in complaint.evidence]
    result += [_task(item, complaints[item.issue_id]) for item in dataset.follow_ups]
    for record in result:  # every field name must exist in the schema contract
        for name in (*record.values, *record.references):
            if name != "Name":
                record.obj.field(name)
    return result


# --- Canonical values and fingerprints ---------------------------------------------


def canonical(field_type: str, value: Any) -> Any:
    """One comparable form for fixture values and values read back from Salesforce."""
    if value is None or value == "":
        return None
    if field_type == "Number":
        return format(Decimal(str(value)).normalize(), "f")
    if field_type == "Checkbox":
        return bool(value)
    if field_type == "Date":
        return value.isoformat() if isinstance(value, date) else str(value)[:10]
    if field_type == "DateTime":
        if isinstance(value, str):
            value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value).replace("\r\n", "\n")


def _field_type(record: SeedRecord, name: str) -> str:
    return "Text" if name == "Name" else record.obj.field(name).type


def canonical_values(record: SeedRecord) -> dict[str, Any]:
    result = {name: canonical(_field_type(record, name), value) for name, value in record.values.items()}
    result.update({name: f"{ref.target}:{ref.key}" for name, ref in record.references.items()})
    return dict(sorted(result.items()))


def fingerprint(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def current_values(record: SeedRecord, row: dict[str, Any], reverse: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Salesforce's current values, mapped into the fixture's canonical form."""
    result = {name: canonical(_field_type(record, name), row.get(name)) for name in record.values}
    for name, ref in record.references.items():
        key = reverse.get(ref.target, {}).get(row.get(name) or "")
        result[name] = f"{ref.target}:{key}" if key else f"other:{row.get(name)}"
    return dict(sorted(result.items()))


# Fields added to the seed mapping after records were first seeded, one set per
# phase, oldest first. Fingerprints written before a set was added do not include
# it, so still-empty values of that set and every later one are not a staff edit.
# Append a new set whenever the mapping of an already-seeded object grows.
ADDED_FIELDS: dict[str, tuple[frozenset[str], ...]] = {
    s.KNOWLEDGE.name: (frozenset({"Policy_Days__c"}),),  # Phase 2
    "Case": (
        frozenset({"NABD_Assessed_Priority__c", "NABD_Priority_Reason__c", "NABD_Summary_Hash__c",
                   "Staff_Action_Required__c", "Staff_Action_Completed__c"}),  # Phase 3
        frozenset({"Negative_Streak__c"}),  # Phase 6
    ),
    s.EVIDENCE.name: (frozenset({"Assessed_Sentiment__c"}),),  # Phase 6
}


def _matches_stored(record: SeedRecord, current: dict[str, Any], stored: str) -> bool:
    if fingerprint(current) == stored:
        return True
    generations = ADDED_FIELDS.get(record.sobject, ())
    # Try "seeded before the newest set", then before the newest two, and so on. An unticked
    # checkbox is the empty value of a newly added checkbox field.
    for start in range(len(generations) - 1, -1, -1):
        added = frozenset().union(*generations[start:]) & current.keys()
        if not added or any(current[name] not in (None, False) for name in added):
            return False
        if fingerprint({name: value for name, value in current.items() if name not in added}) == stored:
            return True
    return False


def decide(record: SeedRecord, row: dict[str, Any] | None, reverse: dict[str, dict[str, str]],
           *, reset: bool) -> Action:
    if row is None:
        return "create"
    desired = fingerprint(canonical_values(record))
    current_map = current_values(record, row, reverse)
    current = fingerprint(current_map)
    stored = row.get(s.SEED_FINGERPRINT)
    if reset:
        return "unchanged" if current == desired and stored == desired else "restore"
    if stored is None or not _matches_stored(record, current_map, stored):
        return "preserve"
    return "unchanged" if desired == stored else "update"


# --- Execution -----------------------------------------------------------------------


@dataclass
class SeedReport:
    counts: dict[str, Counter] = field(default_factory=dict)
    preserved: list[str] = field(default_factory=list)

    def add(self, sobject: str, action: Action, key: str) -> None:
        self.counts.setdefault(sobject, Counter())[action] += 1
        if action == "preserve":
            self.preserved.append(f"{sobject}:{key}")

    @property
    def writes(self) -> int:
        return sum(count[action] for count in self.counts.values()
                   for action in ("create", "update", "restore"))


class Seeder:
    def __init__(self, client: SalesforceClient, *, staff_user_id: str):
        self.client = client
        self.ids: dict[str, dict[str, str]] = {"User": {STAFF_OWNER: staff_user_id}}

    def _reverse(self) -> dict[str, dict[str, str]]:
        return {target: {record_id: key for key, record_id in keys.items()} for target, keys in self.ids.items()}

    async def _load_queues(self) -> None:
        names = [queue.developer_name for queue in s.QUEUES]
        rows = await self.client.query(
            f"SELECT Id, DeveloperName FROM Group WHERE Type = 'Queue' AND DeveloperName IN {literal_list(names)}")
        self.ids["Group"] = {row["DeveloperName"]: row["Id"] for row in rows}
        missing = set(names) - self.ids["Group"].keys()
        if missing:
            raise RuntimeError(f"Deploy the metadata first; missing queues: {', '.join(sorted(missing))}")

    async def _existing(self, sobject: str, keys: list[str], fields: set[str]) -> dict[str, dict[str, Any]]:
        key_field = s.sobject(sobject).seed_key
        selected = ", ".join(sorted({"Id", key_field, s.SEED_FINGERPRINT, *fields}))
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(keys), 100):
            chunk = keys[start:start + 100]
            rows = await self.client.query(
                f"SELECT {selected} FROM {sobject} WHERE {key_field} IN {literal_list(chunk)}")
            for row in rows:
                if row[key_field] in found:
                    raise RuntimeError(f"Duplicate {sobject} records share {key_field} {row[key_field]}")
                found[row[key_field]] = row
        return found

    def _payload(self, record: SeedRecord, *, include_key: bool, creating: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name, value in record.values.items():
            kind = _field_type(record, name)
            if value is None:
                payload[name] = None
            elif kind == "Number":
                payload[name] = float(value) if isinstance(value, Decimal) else value
            elif kind in ("Date", "DateTime"):
                payload[name] = canonical(kind, value)
            else:
                payload[name] = value
        for name, ref in record.references.items():
            if not creating and record.obj.field(name).type == "MasterDetail":
                continue  # master-detail parents are fixed once created and cannot be rewritten
            payload[name] = self.ids[ref.target][ref.key]
        payload[s.SEED_FINGERPRINT] = fingerprint(canonical_values(record))
        if include_key:
            payload[record.obj.seed_key] = record.key
        return payload

    async def run(self, all_records: list[SeedRecord], *, reset: bool = False) -> SeedReport:
        await self._load_queues()
        report = SeedReport()
        order: list[str] = []
        for record in all_records:
            if record.sobject not in order:
                order.append(record.sobject)
        for sobject in order:
            group = [record for record in all_records if record.sobject == sobject]
            fields = {name for record in group for name in (*record.values, *record.references)}
            existing = await self._existing(sobject, [record.key for record in group], fields)
            self.ids[sobject] = {key: row["Id"] for key, row in existing.items()}
            reverse = self._reverse()
            writes: list[tuple[SeedRecord, Action]] = []
            for record in group:
                action = decide(record, existing.get(record.key), reverse, reset=reset)
                report.add(sobject, action, record.key)
                if action in ("create", "update", "restore"):
                    writes.append((record, action))
            await self._write(sobject, writes, existing)
            if writes:  # refresh IDs so children can reference newly created parents
                existing = await self._existing(sobject, [record.key for record in group], set())
                self.ids[sobject] = {key: row["Id"] for key, row in existing.items()}
        return report

    async def _write(self, sobject: str, writes: list[tuple[SeedRecord, Action]],
                     existing: dict[str, dict[str, Any]]) -> None:
        if not writes:
            return
        obj = s.sobject(sobject)
        creates = [self._payload(record, include_key=True) for record, action in writes if action == "create"]
        if obj.field(obj.seed_key).external_id:
            updates = [self._payload(record, include_key=True, creating=False)
                       for record, action in writes if action != "create"]
            for batch in (creates, updates):
                if batch:
                    await self.client.upsert(sobject, obj.seed_key, batch)
            return
        # Task keys cannot be external IDs on activities: create or update by Id instead.
        updates = [{"Id": existing[record.key]["Id"], **self._payload(record, include_key=False, creating=False)}
                   for record, action in writes if action != "create"]
        if creates:
            await self.client.create(sobject, creates)
        if updates:
            await self.client.update(sobject, updates)
