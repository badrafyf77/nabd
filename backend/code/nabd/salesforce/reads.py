"""Narrow, typed, customer-scoped reads used by the NABD backend.

Every customer-data method takes the trusted customer ID from server-side
context, validates it, and filters by it in SOQL. An order owned by another
customer looks exactly like a missing order. Knowledge reads return only
published revisions. Results carry ``RecordRef`` values (record ID plus
``SystemModstamp``) so later agent findings can cite the exact version.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from nabd.salesforce import schema as s
from nabd.salesforce.rest import SalesforceClient
from nabd.salesforce.soql import (
    CUSTOMER_ID, KNOWLEDGE_KEY, ORDER_NUMBER, SALESFORCE_ID, literal, literal_list, require, select,
)

MAX_ORDERS = 50
MAX_CASES = 50
MAX_DEMO_CUSTOMERS = 50
MAX_SYNC_CASES = 100


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class RecordRef(Model):
    sobject: str
    id: str
    version: datetime


class Customer(Model):
    customer_id: str
    first_name: str | None
    last_name: str
    email: str | None
    phone: str | None
    city: str | None
    preferred_language: str | None
    record: RecordRef


class CustomerListing(Model):
    """A seeded customer offered by the demo sign-in picker."""

    customer_id: str
    first_name: str | None
    last_name: str
    preferred_language: str | None


class OrderItem(Model):
    line_number: int
    product: str
    category: str
    quantity: int
    unit_price_gbp: Decimal
    returnable: bool
    record: RecordRef


class Delivery(Model):
    status: str
    carrier: str | None
    tracking_number: str | None
    shipped_date: date | None
    expected_date: date | None
    original_expected_date: date | None
    delivered_date: date | None
    last_location: str | None
    last_update_date: date | None
    record: RecordRef


class Order(Model):
    order_number: str
    order_date: date
    status: str
    total_gbp: Decimal
    payment_method: str | None
    delivery: Delivery | None
    items: tuple[OrderItem, ...] = ()
    record: RecordRef


class CaseSummary(Model):
    case_number: str
    subject: str | None
    status: str
    priority: str | None
    category: str | None
    order_number: str | None
    opened_at: datetime
    closed_at: datetime | None
    is_closed: bool
    record: RecordRef


class CaseHistoryEntry(Model):
    """A case with the context the Complaint stage needs to match a new report to it.

    Holds the staff-facing English summary but no internal assessment
    (sentiment, urgency, confidence, review flag, priority reasoning).
    """

    case_number: str
    subject: str | None
    status: str
    category: str | None
    order_number: str | None
    opened_at: datetime
    closed_at: datetime | None
    is_closed: bool
    summary: str | None
    reopen_count: int
    last_reopened_at: datetime | None
    resolution: str | None
    staff_action: str | None
    staff_action_completed: bool
    record: RecordRef


class PublicReply(Model):
    """A public case comment: a staff reply the customer may see. Private notes are never read."""

    comment_id: str
    body: str
    posted_at: datetime
    edited_at: datetime | None


class CaseState(Model):
    """The customer-visible state of one case, for staff follow-up (Phase 5).

    ``customer_id`` is the case's current owner; the caller shows the state only
    to that customer. No internal assessment fields are read.
    """

    case_id: str
    case_number: str
    customer_id: str | None
    status: str
    is_closed: bool
    resolution: str | None
    staff_action: str | None
    staff_action_completed: bool
    replies: tuple[PublicReply, ...]
    record: RecordRef


class KnowledgeRevision(Model):
    entry_key: str
    revision: int
    revision_key: str
    entry_type: str
    title: str
    question: str | None
    body: str
    topic: str | None
    language: str
    published_at: datetime | None
    policy_days: int | None
    record: RecordRef


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    # Salesforce uses 2026-11-18T09:00:00.000+0000
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _ref(sobject: str, record: dict[str, Any]) -> RecordRef:
    return RecordRef(sobject=sobject, id=record["Id"], version=_datetime(record["SystemModstamp"]))


_DELIVERY_FIELDS = select(
    s.DELIVERY.name, "Id", "SystemModstamp", "Status__c", "Carrier__c", "Tracking_Number__c",
    "Shipped_Date__c", "Expected_Date__c", "Original_Expected_Date__c", "Delivered_Date__c",
    "Last_Location__c", "Last_Update_Date__c",
)
_ITEM_FIELDS = select(
    s.ORDER_ITEM.name, "Id", "SystemModstamp", "Name", "Line_Number__c", "Category__c", "Quantity__c",
    "Unit_Price_GBP__c", "Returnable__c",
)
_ORDER_FIELDS = select(
    s.ORDER.name, "Id", "SystemModstamp", "Order_Number__c", "Order_Date__c", "Status__c", "Total_GBP__c",
    "Payment_Method__c",
)
_CUSTOMER_FIELDS = select(
    s.CONTACT.name, "Id", "SystemModstamp", "ShopEasy_Customer_Id__c", "FirstName", "LastName", "Email",
    "Phone", "MailingCity", "Account.Preferred_Language__c",
)
_CASE_FIELDS = select(
    s.CASE.name, "Id", "SystemModstamp", "CaseNumber", "Subject", "Status", "Priority",
    "Complaint_Category__c", "ShopEasy_Order__r.Order_Number__c", "CreatedDate", "ClosedDate", "IsClosed",
)
_CASE_HISTORY_FIELDS = select(
    s.CASE.name, "Id", "SystemModstamp", "CaseNumber", "Subject", "Status", "Complaint_Category__c",
    "ShopEasy_Order__r.Order_Number__c", "CreatedDate", "ClosedDate", "IsClosed", "English_Summary__c",
    "Reopen_Count__c", "Last_Reopened_At__c", "Resolution_Outcome__c", "Staff_Action_Required__c",
    "Staff_Action_Completed__c",
)
_SYNC_CASE_FIELDS = select(
    s.CASE.name, "Id", "SystemModstamp", "CaseNumber", "Status", "IsClosed", "Account.ShopEasy_Customer_Id__c",
    "Resolution_Outcome__c", "Staff_Action_Required__c", "Staff_Action_Completed__c",
)
_REPLY_FIELDS = select(s.CASE_COMMENT.name, "Id", "CommentBody", "CreatedDate", "LastModifiedDate")
_KNOWLEDGE_FIELDS = select(
    s.KNOWLEDGE.name, "Id", "SystemModstamp", "Name", "Entry_Key__c", "Revision__c", "Revision_Key__c",
    "Entry_Type__c", "Question__c", "Body__c", "Topic__c", "Language__c", "Published_At__c",
    "Policy_Days__c",
)
_LISTING_FIELDS = select(
    s.CONTACT.name, "ShopEasy_Customer_Id__c", "FirstName", "LastName", "Account.Preferred_Language__c",
)
_DELIVERIES = f"{s.DELIVERY.field('Order__c').relationship_name}__r"
_ITEMS = f"{s.ORDER_ITEM.field('Order__c').relationship_name}__r"


def _delivery(record: dict[str, Any]) -> Delivery | None:
    deliveries = record.get(_DELIVERIES) or []
    if not deliveries:
        return None
    row = deliveries[0]
    return Delivery(
        status=row["Status__c"], carrier=row["Carrier__c"], tracking_number=row["Tracking_Number__c"],
        shipped_date=_date(row["Shipped_Date__c"]), expected_date=_date(row["Expected_Date__c"]),
        original_expected_date=_date(row["Original_Expected_Date__c"]),
        delivered_date=_date(row["Delivered_Date__c"]), last_location=row["Last_Location__c"],
        last_update_date=_date(row["Last_Update_Date__c"]), record=_ref(s.DELIVERY.name, row),
    )


def _order(record: dict[str, Any]) -> Order:
    items = tuple(
        OrderItem(
            line_number=int(row["Line_Number__c"]), product=row["Name"], category=row["Category__c"],
            quantity=int(row["Quantity__c"]), unit_price_gbp=_decimal(row["Unit_Price_GBP__c"]),
            returnable=bool(row["Returnable__c"]), record=_ref(s.ORDER_ITEM.name, row),
        )
        for row in sorted(record.get(_ITEMS) or [], key=lambda item: item["Line_Number__c"])
    )
    return Order(
        order_number=record["Order_Number__c"], order_date=_date(record["Order_Date__c"]),
        status=record["Status__c"], total_gbp=_decimal(record["Total_GBP__c"]),
        payment_method=record["Payment_Method__c"], delivery=_delivery(record), items=items,
        record=_ref(s.ORDER.name, record),
    )


def _knowledge(record: dict[str, Any]) -> KnowledgeRevision:
    return KnowledgeRevision(
        entry_key=record["Entry_Key__c"], revision=int(record["Revision__c"]),
        revision_key=record["Revision_Key__c"], entry_type=record["Entry_Type__c"], title=record["Name"],
        question=record["Question__c"], body=record["Body__c"] or "", topic=record["Topic__c"],
        language=record["Language__c"], published_at=_datetime(record["Published_At__c"]),
        policy_days=None if record.get("Policy_Days__c") is None else int(record["Policy_Days__c"]),
        record=_ref(s.KNOWLEDGE.name, record),
    )


class SalesforceReads:
    def __init__(self, client: SalesforceClient):
        self.client = client

    async def get_customer(self, customer_id: str) -> Customer | None:
        customer_id = require(CUSTOMER_ID, customer_id, "customer ID")
        rows = await self.client.query(
            f"SELECT {_CUSTOMER_FIELDS} FROM Contact "
            f"WHERE ShopEasy_Customer_Id__c = {literal(customer_id)} LIMIT 2"
        )
        if len(rows) != 1:
            return None
        row = rows[0]
        return Customer(
            customer_id=row["ShopEasy_Customer_Id__c"], first_name=row["FirstName"], last_name=row["LastName"],
            email=row["Email"], phone=row["Phone"], city=row["MailingCity"],
            preferred_language=(row.get("Account") or {}).get("Preferred_Language__c"),
            record=_ref(s.CONTACT.name, row),
        )

    async def list_demo_customers(self) -> list[CustomerListing]:
        """Seeded customers for the demo sign-in picker; not a customer-scoped read."""
        rows = await self.client.query(
            f"SELECT {_LISTING_FIELDS} FROM Contact WHERE ShopEasy_Customer_Id__c != null "
            f"ORDER BY ShopEasy_Customer_Id__c LIMIT {MAX_DEMO_CUSTOMERS}"
        )
        return [
            CustomerListing(
                customer_id=row["ShopEasy_Customer_Id__c"], first_name=row["FirstName"],
                last_name=row["LastName"],
                preferred_language=(row.get("Account") or {}).get("Preferred_Language__c"),
            )
            for row in rows if CUSTOMER_ID.fullmatch(row["ShopEasy_Customer_Id__c"] or "")
        ]

    async def list_orders(self, customer_id: str) -> list[Order]:
        """The customer's recent orders with delivery status and items."""
        customer_id = require(CUSTOMER_ID, customer_id, "customer ID")
        rows = await self.client.query(
            f"SELECT {_ORDER_FIELDS}, (SELECT {_DELIVERY_FIELDS} FROM {_DELIVERIES} LIMIT 1), "
            f"(SELECT {_ITEM_FIELDS} FROM {_ITEMS} ORDER BY Line_Number__c LIMIT 100) "
            f"FROM {s.ORDER.name} WHERE Account__r.ShopEasy_Customer_Id__c = {literal(customer_id)} "
            f"ORDER BY Order_Date__c DESC, Order_Number__c DESC LIMIT {MAX_ORDERS}"
        )
        return [_order(row) for row in rows]

    async def get_order(self, customer_id: str, order_number: str) -> Order | None:
        """One order with items and delivery, or None if this customer does not own it."""
        customer_id = require(CUSTOMER_ID, customer_id, "customer ID")
        order_number = require(ORDER_NUMBER, order_number, "order number")
        rows = await self.client.query(
            f"SELECT {_ORDER_FIELDS}, (SELECT {_DELIVERY_FIELDS} FROM {_DELIVERIES} LIMIT 1), "
            f"(SELECT {_ITEM_FIELDS} FROM {_ITEMS} ORDER BY Line_Number__c LIMIT 100) "
            f"FROM {s.ORDER.name} WHERE Order_Number__c = {literal(order_number)} "
            f"AND Account__r.ShopEasy_Customer_Id__c = {literal(customer_id)} LIMIT 1"
        )
        return _order(rows[0]) if rows else None

    async def list_cases(self, customer_id: str) -> list[CaseSummary]:
        customer_id = require(CUSTOMER_ID, customer_id, "customer ID")
        rows = await self.client.query(
            f"SELECT {_CASE_FIELDS} FROM Case "
            f"WHERE Account.ShopEasy_Customer_Id__c = {literal(customer_id)} "
            f"ORDER BY CreatedDate DESC LIMIT {MAX_CASES}"
        )
        return [
            CaseSummary(
                case_number=row["CaseNumber"], subject=row["Subject"], status=row["Status"],
                priority=row["Priority"], category=row["Complaint_Category__c"],
                order_number=(row.get("ShopEasy_Order__r") or {}).get("Order_Number__c"),
                opened_at=_datetime(row["CreatedDate"]), closed_at=_datetime(row["ClosedDate"]),
                is_closed=bool(row["IsClosed"]), record=_ref(s.CASE.name, row),
            )
            for row in rows
        ]

    async def list_case_history(self, customer_id: str) -> list[CaseHistoryEntry]:
        """The customer's cases with summaries, newest first, for semantic case matching."""
        customer_id = require(CUSTOMER_ID, customer_id, "customer ID")
        rows = await self.client.query(
            f"SELECT {_CASE_HISTORY_FIELDS} FROM Case "
            f"WHERE Account.ShopEasy_Customer_Id__c = {literal(customer_id)} "
            f"ORDER BY CreatedDate DESC LIMIT {MAX_CASES}"
        )
        return [
            CaseHistoryEntry(
                case_number=row["CaseNumber"], subject=row["Subject"], status=row["Status"],
                category=row["Complaint_Category__c"],
                order_number=(row.get("ShopEasy_Order__r") or {}).get("Order_Number__c"),
                opened_at=_datetime(row["CreatedDate"]), closed_at=_datetime(row["ClosedDate"]),
                is_closed=bool(row["IsClosed"]), summary=row["English_Summary__c"],
                reopen_count=int(row["Reopen_Count__c"] or 0), last_reopened_at=_datetime(row["Last_Reopened_At__c"]),
                resolution=row["Resolution_Outcome__c"], staff_action=row["Staff_Action_Required__c"],
                staff_action_completed=bool(row["Staff_Action_Completed__c"]), record=_ref(s.CASE.name, row),
            )
            for row in rows
        ]

    async def case_states(self, case_ids: list[str]) -> dict[str, CaseState]:
        """Current customer-visible state of these cases, by case ID; deleted cases are absent.

        Used by staff follow-up, which is not a customer request: the caller
        must show a state only to the customer in ``CaseState.customer_id``.
        Only public comments are queried (``IsPublished = true`` is part of the
        query), so private staff notes never leave Salesforce.
        """
        ids = sorted({require(SALESFORCE_ID, case_id, "case ID") for case_id in case_ids})
        states: dict[str, CaseState] = {}
        for start in range(0, len(ids), MAX_SYNC_CASES):
            selected = literal_list(ids[start:start + MAX_SYNC_CASES])
            rows = await self.client.query(f"SELECT {_SYNC_CASE_FIELDS} FROM Case WHERE Id IN {selected}")
            # A separate paginated query avoids silently truncating a long comment history.
            comments = await self.client.query(
                f"SELECT ParentId, {_REPLY_FIELDS} FROM CaseComment "
                f"WHERE ParentId IN {selected} AND IsPublished = true ORDER BY CreatedDate")
            for row in rows:
                replies = tuple(
                    PublicReply(comment_id=item["Id"], body=item["CommentBody"] or "",
                                posted_at=_datetime(item["CreatedDate"]),
                                edited_at=_datetime(item.get("LastModifiedDate")))
                    for item in comments if item["ParentId"] == row["Id"] and (item.get("CommentBody") or "").strip()
                )
                states[row["Id"]] = CaseState(
                    case_id=row["Id"], case_number=row["CaseNumber"],
                    customer_id=(row.get("Account") or {}).get("ShopEasy_Customer_Id__c"),
                    status=row["Status"], is_closed=bool(row["IsClosed"]), resolution=row["Resolution_Outcome__c"],
                    staff_action=row["Staff_Action_Required__c"],
                    staff_action_completed=bool(row["Staff_Action_Completed__c"]), replies=replies,
                    record=_ref(s.CASE.name, row),
                )
        return states

    async def list_published_knowledge(
        self, entry_type: Literal["faq", "policy"] | None = None,
    ) -> list[KnowledgeRevision]:
        """The current published revision of each FAQ/policy entry."""
        condition = "Status__c = 'Published'"
        if entry_type is not None:
            if entry_type not in {value.api for value in s.KNOWLEDGE_TYPES}:
                raise ValueError("unknown knowledge entry type")
            condition += f" AND Entry_Type__c = {literal(entry_type)}"
        rows = await self.client.query(
            f"SELECT {_KNOWLEDGE_FIELDS} FROM {s.KNOWLEDGE.name} WHERE {condition} "
            f"ORDER BY Entry_Key__c, Revision__c DESC LIMIT 500"
        )
        current: dict[str, KnowledgeRevision] = {}
        for row in rows:
            entry = _knowledge(row)
            # The Apex trigger keeps one published revision; prefer the newest if a race left two.
            current.setdefault(entry.entry_key, entry)
        return list(current.values())

    async def get_published_knowledge(self, entry_key: str) -> KnowledgeRevision | None:
        entry_key = require(KNOWLEDGE_KEY, entry_key, "knowledge entry key")
        rows = await self.client.query(
            f"SELECT {_KNOWLEDGE_FIELDS} FROM {s.KNOWLEDGE.name} "
            f"WHERE Entry_Key__c = {literal(entry_key)} AND Status__c = 'Published' "
            f"ORDER BY Revision__c DESC LIMIT 1"
        )
        return _knowledge(rows[0]) if rows else None
