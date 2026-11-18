"""Synthetic ShopEasy fixtures for the Salesforce development org.

These are deployment/test inputs only; the running product reads Salesforce.
The original 10 customers and 25 orders come unchanged from ``shop.seed`` (the
legacy voice prototype's scenarios). Six Moroccan and French customers, their
orders and representative multilingual complaints are added here.

Every date is an offset from ``SEED_DATE``, so a seed is reproducible: the
same seed date always produces the same records. All amounts are GBP and all
customers share ShopEasy's one set of UK policies, whatever their language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from shop import seed as legacy

SEED_DATE = date(2026, 11, 18)


@dataclass(frozen=True)
class Customer:
    customer_id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    city: str
    language: str


@dataclass(frozen=True)
class Item:
    line_number: int
    product: str
    category: str
    quantity: int
    unit_price: Decimal
    returnable: bool


@dataclass(frozen=True)
class Delivery:
    status: str
    carrier: str | None
    tracking_number: str | None
    shipped: date | None
    expected: date | None
    original_expected: date | None
    delivered: date | None
    last_location: str | None
    last_update: date | None


@dataclass(frozen=True)
class Order:
    order_number: str
    customer_id: str
    order_date: date
    status: str
    total: Decimal
    payment_method: str
    items: tuple[Item, ...]
    delivery: Delivery


@dataclass(frozen=True)
class Knowledge:
    entry_key: str
    revision: int
    entry_type: str
    status: str
    title: str
    body: str
    topic: str | None = None
    question: str | None = None
    keywords: str | None = None
    published_at: datetime | None = None
    retired_at: datetime | None = None
    policy_days: int | None = None  # structured value that backend checks read (return window)

    @property
    def revision_key(self) -> str:
        return f"{self.entry_key}@{self.revision}"


@dataclass(frozen=True)
class Evidence:
    message_id: str
    kind: str
    text: str
    language: str | None
    script: str | None
    captured_at: datetime
    verified_source: str | None = None


@dataclass(frozen=True)
class Complaint:
    issue_id: str
    customer_id: str
    order_number: str | None
    subject: str
    category: str
    status: str
    priority: str
    language: str
    sentiment: str
    urgency: str
    confidence: Decimal
    summary: str
    evidence: tuple[Evidence, ...]
    conversation_id: str
    needs_review: bool = False
    outcome: str | None = None
    confirmation_message_id: str | None = None
    confirmed_at: datetime | None = None
    # Phase 3: the policy reason behind the priority and any staff-only action.
    priority_reason: str | None = None
    staff_action: str | None = None
    staff_action_completed: bool = False


@dataclass(frozen=True)
class FollowUp:
    task_key: str
    issue_id: str
    subject: str
    description: str
    due: date


@dataclass(frozen=True)
class RiskRule:
    code: str
    version: int
    name: str
    description: str
    parameters: str

    @property
    def rule_key(self) -> str:
        return f"{self.code}@{self.version}"


@dataclass(frozen=True)
class Dataset:
    seed_date: date
    customers: tuple[Customer, ...]
    orders: tuple[Order, ...]
    knowledge: tuple[Knowledge, ...]
    complaints: tuple[Complaint, ...]
    follow_ups: tuple[FollowUp, ...]
    risk_rules: tuple[RiskRule, ...]


# --- Customers -------------------------------------------------------------------

# Moroccan and French customers live in the UK: ShopEasy only delivers there.
NEW_CUSTOMERS = (
    ("C011", "Youssef", "El Amrani", "07700900111", "youssef.elamrani@example.com", "London", "ary"),
    ("C012", "Fatima Zahra", "Bennani", "07700900112", "fatimazahra.bennani@example.com", "Manchester", "ar"),
    ("C013", "Hamza", "Tazi", "07700900113", "hamza.tazi@example.com", "Birmingham", "ary"),
    ("C014", "Camille", "Dubois", "07700900114", "camille.dubois@example.com", "London", "fr"),
    ("C015", "Julien", "Moreau", "07700900115", "julien.moreau@example.com", "Brighton", "fr"),
    ("C016", "Salma", "Ouazzani", "07700900116", "salma.ouazzani@example.com", "Leeds", "ary"),
)

# Same tuple format as ``shop.seed.ORDERS`` so both use one conversion.
NEW_ORDERS = (
    # C011 Youssef (Darija, Latin script): a delayed parcel.
    ("SE-1141", "C011", -9, "Visa ending 3318",
     [("Electric hair clipper", "beauty", 1, 39.99, 1)],
     ("delayed", -8, 1, -2, None, "London depot, held due to high volume")),
    ("SE-1142", "C011", -20, "Visa ending 3318",
     [("Running jacket", "sports", 1, 59.00, 1)],
     ("delivered", -18, -16, -16, -16, "Delivered to front door")),
    # C012 Fatima Zahra (Arabic): a kettle that arrived broken two days ago.
    ("SE-1143", "C012", -5, "Mastercard ending 6604",
     [("Glass electric kettle", "kitchen", 1, 42.00, 1), ("Tea glasses, set of 6", "kitchen", 1, 18.00, 1)],
     ("delivered", -4, -2, -2, -2, "Delivered to front door")),
    # C013 Hamza (French/Darija mix): reports being charged twice.
    ("SE-1144", "C013", -6, "Visa ending 7781",
     [("Wireless earbuds", "electronics", 1, 79.99, 1)],
     ("in_transit", -3, 1, 1, None, "Birmingham sorting centre")),
    ("SE-1145", "C013", -40, "Visa ending 7781",
     [("Phone charger", "electronics", 2, 14.99, 1)],
     ("delivered", -38, -36, -36, -36, "Delivered to parcel locker")),
    # C014 Camille (French): a lost parcel, resolved by a delivered replacement.
    ("SE-1146", "C014", -25, "PayPal",
     [("Linen bed sheets", "home", 1, 69.00, 1)],
     ("lost", -24, -21, -21, None, "Last scanned at Croydon hub")),
    ("SE-1147", "C014", -12, "PayPal",
     [("Linen bed sheets", "home", 1, 69.00, 1)],
     ("delivered", -11, -9, -9, -9, "Delivered to front door")),
    # C015 Julien (French): returned boots and is waiting for a refund.
    ("SE-1148", "C015", -26, "Amex ending 4410",
     [("Leather ankle boots", "clothing", 1, 110.00, 1)],
     ("delivered", -24, -22, -22, -22, "Delivered to reception")),
    ("SE-1149", "C015", -1, "Amex ending 4410",
     [("Wool scarf", "clothing", 1, 29.00, 1)],
     ("preparing", None, 4, 4, None, "ShopEasy warehouse")),
    # C016 Salma (Darija, Arabic script): a general service complaint, no order involved.
    ("SE-1150", "C016", -10, "Mastercard ending 1290",
     [("Argan oil gift set", "beauty", 1, 32.50, 1), ("Personalised phone case", "personalised", 1, 16.00, 0)],
     ("delivered", -8, -7, -7, -7, "Delivered to neighbour at number 4")),
)


def _day(seed_date: date, offset: int | None) -> date | None:
    return None if offset is None else seed_date + timedelta(days=offset)


def _at(seed_date: date, offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(seed_date + timedelta(days=offset), time(hour, minute), tzinfo=timezone.utc)


def _orders(seed_date: date) -> tuple[Order, ...]:
    """Convert legacy-format order tuples exactly as ``shop.seed.build_database`` does."""
    orders = []
    for index, (order_id, customer_id, ordered, payment, items, delivery) in enumerate(
        (*legacy.ORDERS, *NEW_ORDERS)
    ):
        status, shipped, expected, original, delivered, location = delivery
        total = round(sum(qty * price for _, _, qty, price, _ in items), 2)
        shipped_at_all = shipped is not None
        orders.append(Order(
            order_number=order_id,
            customer_id=customer_id,
            order_date=_day(seed_date, ordered),
            status=legacy.ORDER_STATUS_FOR_DELIVERY[status],
            total=Decimal(str(total)),
            payment_method=payment,
            items=tuple(
                Item(line, product, category, qty, Decimal(str(price)), bool(returnable))
                for line, (product, category, qty, price, returnable) in enumerate(items, start=1)
            ),
            delivery=Delivery(
                status=status,
                carrier=legacy.CARRIERS[index % len(legacy.CARRIERS)] if shipped_at_all else None,
                tracking_number=f"RP{order_id[3:]}{customer_id[1:]}GB" if shipped_at_all else None,
                shipped=_day(seed_date, shipped),
                expected=_day(seed_date, expected),
                original_expected=_day(seed_date, original),
                delivered=_day(seed_date, delivered),
                last_location=location,
                last_update=_day(seed_date, legacy.last_update_offset(status, ordered, shipped, delivered)),
            ),
        ))
    return tuple(orders)


# --- Knowledge -------------------------------------------------------------------

# Legacy FAQ answers were written to be spoken aloud ("3 pounds 99").
# Text chat shows the same facts in written form.
_SPOKEN_TO_TEXT = (
    (re.compile(r"(\d+) pounds (\d{2})\b"), r"£\1.\2"),
    (re.compile(r"(\d+) pounds\b"), r"£\1"),
    (re.compile(r"(\d+) percent\b"), r"\1%"),
    (re.compile(r"help at shopeasy dot example"), "help@shopeasy.example"),
)


def text_form(spoken: str) -> str:
    for pattern, replacement in _SPOKEN_TO_TEXT:
        spoken = pattern.sub(replacement, spoken)
    return spoken


# One key per legacy FAQ, in the same order as ``shop.seed.FAQ``.
FAQ_KEYS = (
    "faq.delivery_cost", "faq.delivery_time", "faq.international_delivery", "faq.change_address",
    "faq.cancel_order", "faq.payment_methods", "faq.track_order", "faq.gift_wrapping",
    "faq.support_hours", "faq.forgot_password", "faq.price_match", "faq.student_discount",
    "faq.gift_cards", "faq.contact_email",
)
POLICY_TITLES = {
    "window": "Return window", "condition": "Condition of returned items",
    "non_returnable": "Items that cannot be returned", "how_to_return": "How to return an item",
    "refunds": "Refunds", "exchanges": "Exchanges", "damaged_items": "Damaged or wrong items",
    "cancelled_orders": "Cancelled orders",
}


def _knowledge(seed_date: date) -> tuple[Knowledge, ...]:
    published = _at(seed_date, -60, 9)
    entries = []
    for key, (question, answer, keywords) in zip(FAQ_KEYS, legacy.FAQ, strict=True):
        revision = 2 if key == "faq.delivery_cost" else 1
        entries.append(Knowledge(
            entry_key=key, revision=revision, entry_type="faq", status="Published", title=question,
            question=question, body=text_form(answer), keywords=keywords, topic=key.removeprefix("faq."),
            published_at=_at(seed_date, -30, 9) if revision == 2 else published,
        ))
    for topic, text in legacy.RETURN_POLICY:
        entries.append(Knowledge(
            entry_key=f"policy.{'return_window' if topic == 'window' else topic}", revision=1,
            entry_type="policy", status="Published", title=POLICY_TITLES[topic], body=text, topic=topic,
            published_at=published, policy_days=legacy.RETURN_WINDOW_DAYS if topic == "window" else None,
        ))
    # Revision history: an older delivery price was retired when revision 2 went live.
    entries.append(Knowledge(
        entry_key="faq.delivery_cost", revision=1, entry_type="faq", status="Retired",
        title="How much does delivery cost?", question="How much does delivery cost?",
        body="Standard delivery costs £2.99 and is free on orders over £40. Next-day delivery costs £5.99.",
        keywords=legacy.FAQ[0][2], topic="delivery_cost", published_at=published,
        retired_at=_at(seed_date, -30, 9),
    ))
    # A staff draft that must never reach customers until it is published.
    entries.append(Knowledge(
        entry_key="faq.support_hours", revision=2, entry_type="faq", status="Draft",
        title="When is customer support open?", question="When is customer support open?",
        body="Chat support is open 24 hours a day. Our team answers escalated cases every day "
             "from 8 am to 10 pm.",
        keywords=legacy.FAQ[8][2], topic="support_hours",
    ))
    return tuple(entries)


# --- Complaints ------------------------------------------------------------------


def _complaints(seed_date: date) -> tuple[Complaint, ...]:
    at = lambda offset, hour, minute=0: _at(seed_date, offset, hour, minute)  # noqa: E731
    return (
        Complaint(
            issue_id="FX-ISSUE-0001", customer_id="C004", order_number="SE-1071",
            subject="Smart watch order SE-1071 lost in transit", category="Delivery", status="Working",
            priority="Medium", language="en", sentiment="negative", urgency="medium",
            confidence=Decimal("0.940"), conversation_id="fixture-conv-0001",
            priority_reason="Medium: lost parcel reported", staff_action="carrier_investigation",
            summary="Customer reports that smart watch order SE-1071 has not arrived and tracking stopped "
                    "at the London hub. The delivery record shows the parcel as lost. A carrier "
                    "investigation is open.",
            evidence=(
                Evidence("FX-MSG-0001", "customer_report",
                         "My smart watch order SE-1071 still hasn't arrived. Tracking says it was last "
                         "scanned at the London hub over a week ago. Can you find out what happened?",
                         "en", "latin", at(-2, 14, 5)),
                Evidence("FX-FACT-0001", "verified_fact",
                         "Delivery SE-1071 has status Lost; last scan at London hub.",
                         "en", "latin", at(-2, 14, 6), verified_source="ShopEasy_Delivery__c:SE-1071"),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0002", customer_id="C011", order_number="SE-1141",
            subject="Order SE-1141 delayed", category="Delivery", status="Waiting on Customer",
            priority="Low", language="ary", sentiment="negative", urgency="low",
            confidence=Decimal("0.910"), conversation_id="fixture-conv-0002",
            priority_reason="Low: routine delivery delay",
            summary="Customer says order SE-1141 should have arrived two days ago and tracking now shows "
                    "it as delayed. He asked for the new delivery date; we are waiting for him to say "
                    "whether he wants to keep the order.",
            evidence=(
                Evidence("FX-MSG-0002", "customer_report",
                         "Salam, l commande dyali SE-1141 mazal ma wsltch. Kan khassha twsel hadi joj "
                         "iyam w daba kayban ghir 'delayed'. Imta ghadi twsel?",
                         "ary", "latin", at(-1, 18, 40)),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0003", customer_id="C012", order_number="SE-1143",
            subject="Glass kettle from SE-1143 arrived broken", category="Orders_Products",
            status="Waiting on Staff", priority="Low", language="ar", sentiment="negative", urgency="medium",
            confidence=Decimal("0.880"), conversation_id="fixture-conv-0003",
            priority_reason="Low: damaged item without a stronger signal", staff_action="replacement",
            summary="Customer reports that the glass electric kettle in order SE-1143, delivered two days "
                    "ago, arrived broken and cannot be used. She asks for a replacement. Reported within "
                    "the 7-day damaged-item window; staff must approve the replacement.",
            evidence=(
                Evidence("FX-MSG-0003", "customer_report",
                         "السلام عليكم، وصلني الطلب SE-1143 قبل يومين لكن الغلاية الزجاجية مكسورة ولا "
                         "يمكن استعمالها. أرجو استبدالها في أقرب وقت.",
                         "ar", "arabic", at(0, 9, 12)),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0004", customer_id="C013", order_number="SE-1144",
            subject="Possible duplicate charge for SE-1144", category="Billing_Payments", status="New",
            priority="High", language="mixed", sentiment="negative", urgency="high",
            confidence=Decimal("0.900"), conversation_id="fixture-conv-0004",
            priority_reason="High: suspected duplicate charge", staff_action="billing_review",
            summary="Customer reports being charged twice (2 x £79.99) for order SE-1144 although he paid "
                    "once. This is an unverified customer report of a duplicate charge, so the case is "
                    "High priority for Billing.",
            evidence=(
                Evidence("FX-MSG-0004", "customer_report",
                         "Bonjour, j'ai été débité deux fois pour la commande SE-1144, 79,99 £ x2 sur ma "
                         "carte. 3afak checkiw had l'mochkil, ana khllst ghir mra wahda.",
                         "mixed", "latin", at(0, 11, 30)),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0005", customer_id="C014", order_number="SE-1146",
            subject="Lost parcel SE-1146 replaced", category="Delivery", status="Closed", priority="Medium",
            language="fr", sentiment="positive", urgency="medium", confidence=Decimal("0.950"),
            conversation_id="fixture-conv-0005",
            priority_reason="Medium: lost parcel reported", staff_action="replacement",
            staff_action_completed=True, outcome="resolved",
            confirmation_message_id="FX-MSG-0006", confirmed_at=at(-8, 16, 20),
            summary="Customer reported that order SE-1146 was marked lost. The delivery record confirmed "
                    "the loss and replacement order SE-1147 was sent. The customer confirmed she received "
                    "the replacement and that the issue is resolved.",
            evidence=(
                Evidence("FX-MSG-0005", "customer_report",
                         "Bonjour, ma commande SE-1146 devait arriver il y a plusieurs jours et le suivi "
                         "indique maintenant « perdu ». Que pouvez-vous faire ?",
                         "fr", "latin", at(-20, 10, 2)),
                Evidence("FX-FACT-0005", "verified_fact",
                         "Delivery SE-1146 has status Lost; replacement order SE-1147 was delivered.",
                         "en", "latin", at(-9, 12, 0), verified_source="ShopEasy_Delivery__c:SE-1147"),
                Evidence("FX-MSG-0006", "customer_confirmation",
                         "Merci, j'ai bien reçu les draps de remplacement (commande SE-1147). Tout est "
                         "réglé pour moi.",
                         "fr", "latin", at(-8, 16, 20)),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0006", customer_id="C015", order_number="SE-1148",
            subject="Refund pending for returned boots (SE-1148)", category="Returns_Refunds",
            status="Waiting on Staff", priority="Low", language="fr", sentiment="neutral", urgency="low",
            confidence=Decimal("0.930"), conversation_id="fixture-conv-0006",
            priority_reason="Low: routine refund request", staff_action="refund",
            summary="Customer says he returned the leather ankle boots from order SE-1148 ten days ago with "
                    "the prepaid label and has not been refunded. Refunds are approved by staff; a staff "
                    "follow-up checks the return receipt.",
            evidence=(
                Evidence("FX-MSG-0007", "customer_report",
                         "Bonjour, j'ai renvoyé les bottines de la commande SE-1148 il y a dix jours avec "
                         "l'étiquette prépayée, mais je n'ai toujours pas été remboursé. Pouvez-vous "
                         "vérifier ?",
                         "fr", "latin", at(-1, 15, 45)),
            ),
        ),
        Complaint(
            issue_id="FX-ISSUE-0007", customer_id="C016", order_number=None,
            subject="Customer says nobody answered her email", category="General_Support", status="New",
            priority="Low", language="ary", sentiment="negative", urgency="low",
            confidence=Decimal("0.410"), needs_review=True, conversation_id="fixture-conv-0007",
            priority_reason="Low: no stronger signal",
            summary="Customer is unhappy that nobody has answered an email she sent last week. She did not "
                    "say what the email was about, so the category is uncertain and staff should review.",
            evidence=(
                Evidence("FX-MSG-0008", "customer_report",
                         "السلام، بقيت كنتسنى شي واحد يجاوبني على الإيميل من السيمانة اللي فاتت وحتى واحد "
                         "ما رد عليا. هادشي ماشي معقول.",
                         "ary", "arabic", at(0, 8, 55)),
            ),
        ),
        # Phase 3: an English case that a later conversation can close with evidence.
        Complaint(
            issue_id="FX-ISSUE-0008", customer_id="C007", order_number="SE-1101",
            subject="Ceramic plant pot SE-1101 marked delivered but not found", category="Delivery",
            status="Waiting on Customer", priority="Low", language="en", sentiment="negative", urgency="low",
            confidence=Decimal("0.920"), conversation_id="fixture-conv-0008",
            priority_reason="Low: routine delivery question",
            summary="Customer says order SE-1101 (ceramic plant pot) shows as delivered on Monday 16 November "
                    "but she cannot find the parcel. We asked her to check with neighbours and her safe "
                    "place before a carrier investigation is opened.",
            evidence=(
                Evidence("FX-MSG-0009", "customer_report",
                         "My order SE-1101 says it was delivered on Monday but I can't find it anywhere. "
                         "Where is my plant pot?",
                         "en", "latin", at(-1, 17, 10)),
                Evidence("FX-FACT-0008", "verified_fact",
                         "Delivery SE-1101 has status Delivered on 16 November.",
                         "en", "latin", at(-1, 17, 11), verified_source="ShopEasy_Delivery__c:SE-1101"),
            ),
        ),
    )


def _follow_ups(seed_date: date) -> tuple[FollowUp, ...]:
    due = seed_date + timedelta(days=2)
    return (
        FollowUp("FX-TASK-0001", "FX-ISSUE-0001", "Open carrier investigation for SE-1071",
                 "Ask the carrier to trace the lost smart watch parcel and update the customer.", due),
        FollowUp("FX-TASK-0002", "FX-ISSUE-0003", "Approve replacement kettle for SE-1143",
                 "Damaged-item report within 7 days. Approve a replacement or full refund; the customer "
                 "does not pay for the return.", due),
        FollowUp("FX-TASK-0003", "FX-ISSUE-0006", "Check return receipt and approve refund for SE-1148",
                 "Confirm the returned boots reached the warehouse, then approve the refund to the original "
                 "payment method.", due),
    )


RISK_RULES = (
    RiskRule("repeat_complaints", 1, "Repeated complaints",
             "Three distinct complaints from one customer within thirty days.",
             '{"distinct_complaints": 3, "window_days": 30}'),
    RiskRule("high_priority_unresolved", 1, "High-priority case unresolved",
             "A High-priority case remains unresolved for twenty-four hours.",
             '{"priority": "High", "unresolved_hours": 24}'),
    RiskRule("persistent_negative_sentiment", 1, "Negative sentiment persists",
             "Sentiment becomes negative and stays negative at the next follow-up for the same unresolved "
             "issue.",
             '{"follow_ups": 1}'),
)


def build(seed_date: date = SEED_DATE) -> Dataset:
    customers = tuple(
        Customer(*row, language="en") for row in legacy.CUSTOMERS
    ) + tuple(Customer(*row[:6], language=row[6]) for row in NEW_CUSTOMERS)
    return Dataset(
        seed_date=seed_date,
        customers=customers,
        orders=_orders(seed_date),
        knowledge=_knowledge(seed_date),
        complaints=_complaints(seed_date),
        follow_ups=_follow_ups(seed_date),
        risk_rules=RISK_RULES,
    )
