"""The NABD Salesforce schema contract.

This module is the one place that names Salesforce objects, fields, picklist
values, queues, groups and record access. Three consumers read it:

* ``metadata`` renders the SFDX source that is deployed to the org;
* ``fixtures``/``seed`` map synthetic ShopEasy data onto these fields;
* ``reads`` builds whitelisted, customer-scoped queries from it.

Standard objects (Account, Contact, Case, Task) are described too, but only
their ``custom`` fields are deployed. Standard fields appear so the seed and
read code can validate every field name it uses against one contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

API_VERSION = "68.0"

FieldType = Literal[
    "Text", "LongTextArea", "Number", "Date", "DateTime", "Checkbox", "Picklist",
    "Lookup", "MasterDetail", "Email", "Phone", "Id", "AutoNumber",
]


@dataclass(frozen=True)
class PicklistValue:
    api: str
    label: str
    default: bool = False
    closed: bool = False  # only meaningful for the Case status value set


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: FieldType
    description: str = ""
    custom: bool = True
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    visible_lines: int | None = None
    required: bool = False
    unique: bool = False
    external_id: bool = False
    case_sensitive: bool = False
    values: tuple[PicklistValue, ...] = ()
    reference_to: str | None = None
    relationship_name: str | None = None
    relationship_label: str | None = None
    delete_constraint: Literal["Restrict", "SetNull", "Cascade"] | None = None
    default_value: str | None = None
    # Standard fields that Salesforce does not let permission sets control.
    permissionable: bool = True
    # The staff permission set grants edit unless the platform owns the value.
    staff_editable: bool = True
    # Only fields marked here are readable by the restricted integration user.
    integration_readable: bool = False
    # Address parts are permissioned through their compound field (BillingAddress).
    permission_field: str | None = None

    @property
    def is_relationship(self) -> bool:
        return self.type in ("Lookup", "MasterDetail")

    @property
    def relationship(self) -> str:
        """Relationship name used in SOQL paths and external-ID upserts."""
        if not self.is_relationship:
            raise ValueError(f"{self.name} is not a relationship field")
        if self.custom:
            return self.name[:-3] + "__r"
        return self.name[:-2]  # AccountId -> Account

    @property
    def grants_field_permission(self) -> bool:
        """Required and master-detail fields are always visible and reject FLS entries."""
        return self.permissionable and not self.required and self.type != "MasterDetail"


@dataclass(frozen=True)
class ValidationRule:
    name: str
    formula: str
    message: str
    description: str
    error_field: str | None = None


@dataclass(frozen=True)
class LayoutSection:
    label: str
    left: tuple[str, ...]
    right: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelatedList:
    child: str
    field: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class SObject:
    name: str
    label: str
    plural_label: str
    description: str
    fields: tuple[Field, ...]
    custom: bool = True
    metadata_name: str | None = None  # Activity holds custom Task fields
    name_label: str = "Name"
    name_type: Literal["Text", "AutoNumber"] = "Text"
    name_format: str | None = None
    sharing_model: Literal["ReadWrite", "Read", "Private", "ControlledByParent"] = "ReadWrite"
    enable_activities: bool = False
    enable_history: bool = False
    seed_key: str | None = None
    tab_motif: str | None = None
    staff_access: Literal["edit", "read"] = "edit"
    integration_readable: bool = False
    validation_rules: tuple[ValidationRule, ...] = ()
    layout_sections: tuple[LayoutSection, ...] = ()
    related_lists: tuple[RelatedList, ...] = ()
    # Standard objects keep their existing layout and gain these additions.
    standard_layout: str | None = None

    @property
    def folder(self) -> str:
        return self.metadata_name or self.name

    def field(self, name: str) -> Field:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(f"{self.name}.{name} is not in the NABD schema contract")

    @property
    def custom_fields(self) -> tuple[Field, ...]:
        return tuple(item for item in self.fields if item.custom)

    @property
    def master(self) -> str | None:
        """Parent object when sharing is controlled by a master-detail relationship."""
        for item in self.fields:
            if item.type == "MasterDetail":
                return item.reference_to
        return None


def _std(name: str, label: str, type: FieldType, *, permissionable: bool = True, required: bool = False,
         reference_to: str | None = None, integration: bool = False, staff_editable: bool = True,
         values: tuple[PicklistValue, ...] = (), permission_field: str | None = None) -> Field:
    return Field(
        name=name, label=label, type=type, custom=False, permissionable=permissionable,
        required=required, reference_to=reference_to, integration_readable=integration,
        staff_editable=staff_editable, values=values, permission_field=permission_field,
    )


def _values(*pairs: tuple[str, str], default: str | None = None) -> tuple[PicklistValue, ...]:
    return tuple(PicklistValue(api, label, default=api == default) for api, label in pairs)


# --- Shared value sets ---------------------------------------------------------

LANGUAGES = _values(
    ("en", "English"), ("fr", "French"), ("ar", "Arabic"), ("ary", "Darija"), ("mixed", "Mixed"),
)
CUSTOMER_LANGUAGES = tuple(value for value in LANGUAGES if value.api != "mixed")
SCRIPTS = _values(("latin", "Latin"), ("arabic", "Arabic"), ("mixed", "Mixed"))

COMPLAINT_CATEGORIES = _values(
    ("Delivery", "Delivery"),
    ("Orders_Products", "Orders & Products"),
    ("Billing_Payments", "Billing & Payments"),
    ("Returns_Refunds", "Returns & Refunds"),
    ("General_Support", "General Support"),
)

CASE_STATUSES = (
    PicklistValue("New", "New", default=True),
    PicklistValue("Working", "Working"),
    PicklistValue("Waiting on Customer", "Waiting on Customer"),
    PicklistValue("Waiting on Staff", "Waiting on Staff"),
    PicklistValue("Closed", "Closed", closed=True),
)
# Existing org values are kept; NABD adds its chat channel.
CASE_ORIGINS = _values(("Phone", "Phone"), ("Email", "Email"), ("Web", "Web"), ("NABD Chat", "NABD Chat"))
CASE_PRIORITIES = _values(("High", "High"), ("Medium", "Medium"), ("Low", "Low"))

ORDER_STATUSES = _values(
    ("processing", "Processing"), ("shipped", "Shipped"), ("delivered", "Delivered"), ("cancelled", "Cancelled"),
)
DELIVERY_STATUSES = _values(
    ("preparing", "Preparing"), ("in_transit", "In transit"), ("out_for_delivery", "Out for delivery"),
    ("delivered", "Delivered"), ("delayed", "Delayed"), ("lost", "Lost"), ("cancelled", "Cancelled"),
)
PRODUCT_CATEGORIES = _values(
    ("electronics", "Electronics"), ("home", "Home"), ("kitchen", "Kitchen"), ("sports", "Sports"),
    ("clothing", "Clothing"), ("underwear", "Underwear"), ("software", "Software"), ("office", "Office"),
    ("travel", "Travel"), ("gift_card", "Gift card"), ("accessories", "Accessories"),
    ("personalised", "Personalised"), ("beauty", "Beauty"),
)
KNOWLEDGE_STATUSES = _values(("Draft", "Draft"), ("Published", "Published"), ("Retired", "Retired"), default="Draft")
KNOWLEDGE_TYPES = _values(("faq", "FAQ"), ("policy", "Policy"))
SENTIMENTS = _values(("positive", "Positive"), ("neutral", "Neutral"), ("negative", "Negative"),
                     ("very_negative", "Very negative"))
URGENCIES = _values(("low", "Low"), ("medium", "Medium"), ("high", "High"))
RESOLUTION_OUTCOMES = _values(
    ("resolved", "Resolved"), ("unresolved", "Not resolved"), ("withdrawn", "Withdrawn by customer"),
    ("duplicate", "Duplicate of another case"),
)
EVIDENCE_KINDS = _values(
    ("customer_report", "Customer report"), ("verified_fact", "Verified fact"),
    ("customer_confirmation", "Customer confirmation"),
)

SEED_FINGERPRINT = "NABD_Seed_Fingerprint__c"


def _fingerprint() -> Field:
    return Field(
        SEED_FINGERPRINT, "NABD Seed Fingerprint", "Text", length=64, staff_editable=False,
        description="Hash of the fixture values written by the last seed. A mismatch means the record "
                    "was edited after seeding, so a normal re-seed preserves it.",
    )


def _ext_id(name: str, label: str, length: int, description: str, *, case_sensitive: bool = False,
            unique: bool = True, required: bool = False, integration: bool = False) -> Field:
    return Field(
        name, label, "Text", length=length, unique=unique, external_id=True, case_sensitive=case_sensitive,
        required=required, description=description, staff_editable=False, integration_readable=integration,
    )


# --- Customers -------------------------------------------------------------------

ACCOUNT = SObject(
    name="Account", label="Account", plural_label="Accounts", custom=False,
    description="One account per ShopEasy customer (individual model; person accounts are not enabled).",
    seed_key="ShopEasy_Customer_Id__c",
    standard_layout="Account-Account Layout",
    integration_readable=True,
    fields=(
        _std("Name", "Account Name", "Text", permissionable=False, required=True, integration=True),
        _std("Phone", "Phone", "Phone", integration=True),
        _std("BillingCity", "Billing City", "Text", integration=True, permission_field="BillingAddress"),
        _std("BillingCountry", "Billing Country", "Text", integration=True, permission_field="BillingAddress"),
        _ext_id("ShopEasy_Customer_Id__c", "ShopEasy Customer ID", 20,
                "Stable ShopEasy customer identifier, such as C001.", integration=True),
        Field("Preferred_Language__c", "Preferred Language", "Picklist", values=CUSTOMER_LANGUAGES,
              integration_readable=True,
              description="The customer's usual language. NABD still follows each message's language."),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("ShopEasy Customer", ("ShopEasy_Customer_Id__c",), ("Preferred_Language__c",)),
    ),
    related_lists=(
        RelatedList("ShopEasy_Order__c", "Account__c", ("NAME", "Order_Date__c", "Status__c", "Total_GBP__c")),
        RelatedList("Risk_Episode__c", "Account__c", ("NAME", "Risk_Rule__c", "Status__c", "Opened_At__c")),
    ),
)

CONTACT = SObject(
    name="Contact", label="Contact", plural_label="Contacts", custom=False,
    description="The person behind a ShopEasy customer account.",
    seed_key="ShopEasy_Customer_Id__c",
    standard_layout="Contact-Contact Layout",
    integration_readable=True,
    fields=(
        _std("FirstName", "First Name", "Text", permissionable=False, integration=True),
        _std("LastName", "Last Name", "Text", permissionable=False, required=True, integration=True),
        _std("Email", "Email", "Email", integration=True),
        _std("Phone", "Phone", "Phone", integration=True),
        _std("MailingCity", "Mailing City", "Text", integration=True, permission_field="MailingAddress"),
        _std("AccountId", "Account", "Lookup", reference_to="Account", integration=True),
        _ext_id("ShopEasy_Customer_Id__c", "ShopEasy Customer ID", 20,
                "Same stable ShopEasy customer identifier as the account.", integration=True),
        _fingerprint(),
    ),
    layout_sections=(LayoutSection("ShopEasy Customer", ("ShopEasy_Customer_Id__c",)),),
)

# --- Orders, items and deliveries ------------------------------------------------

ORDER = SObject(
    name="ShopEasy_Order__c", label="ShopEasy Order", plural_label="ShopEasy Orders",
    description="A ShopEasy order. Amounts are GBP; ShopEasy uses one currency and one policy set.",
    name_label="Order Number", seed_key="Order_Number__c", sharing_model="Read",
    enable_activities=True, enable_history=True, tab_motif="Custom13: Box",
    integration_readable=True,
    fields=(
        _ext_id("Order_Number__c", "Order Number", 20, "Stable ShopEasy order number, such as SE-1042.",
                required=True, integration=True),
        Field("Account__c", "Customer Account", "Lookup", reference_to="Account", required=True,
              relationship_name="ShopEasy_Orders", relationship_label="ShopEasy Orders",
              delete_constraint="Restrict", integration_readable=True,
              description="The ordering customer. Every order belongs to exactly one customer."),
        Field("Contact__c", "Customer Contact", "Lookup", reference_to="Contact",
              relationship_name="ShopEasy_Orders", relationship_label="ShopEasy Orders",
              delete_constraint="SetNull", integration_readable=True),
        Field("Order_Date__c", "Order Date", "Date", required=True, integration_readable=True),
        Field("Status__c", "Order Status", "Picklist", values=ORDER_STATUSES, required=True,
              integration_readable=True),
        Field("Total_GBP__c", "Total (GBP)", "Number", precision=12, scale=2, required=True,
              integration_readable=True, description="Order total in pounds sterling."),
        Field("Payment_Method__c", "Payment Method", "Text", length=80, integration_readable=True,
              description="Masked payment description, such as 'Visa ending 4421'."),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("Order", ("Name", "Order_Number__c", "Account__c", "Contact__c"),
                      ("Order_Date__c", "Status__c", "Total_GBP__c", "Payment_Method__c")),
    ),
    related_lists=(
        RelatedList("ShopEasy_Order_Item__c", "Order__c",
                    ("NAME", "Category__c", "Quantity__c", "Unit_Price_GBP__c", "Returnable__c")),
        RelatedList("ShopEasy_Delivery__c", "Order__c",
                    ("NAME", "Status__c", "Carrier__c", "Expected_Date__c", "Last_Location__c")),
        RelatedList("Case", "ShopEasy_Order__c", ("CASES.CASE_NUMBER", "CASES.SUBJECT", "CASES.STATUS")),
    ),
)

ORDER_ITEM = SObject(
    name="ShopEasy_Order_Item__c", label="ShopEasy Order Item", plural_label="ShopEasy Order Items",
    description="One product line of a ShopEasy order.",
    name_label="Product", seed_key="Item_Key__c", sharing_model="ControlledByParent",
    integration_readable=True,
    fields=(
        Field("Order__c", "Order", "MasterDetail", reference_to="ShopEasy_Order__c", required=True,
              relationship_name="Items", relationship_label="Items", integration_readable=True),
        _ext_id("Item_Key__c", "Item Key", 30, "Order number plus line number, such as SE-1042-1.",
                required=True, integration=True),
        Field("Line_Number__c", "Line Number", "Number", precision=3, scale=0, required=True,
              integration_readable=True),
        Field("Category__c", "Product Category", "Picklist", values=PRODUCT_CATEGORIES, required=True,
              integration_readable=True),
        Field("Quantity__c", "Quantity", "Number", precision=5, scale=0, required=True,
              integration_readable=True),
        Field("Unit_Price_GBP__c", "Unit Price (GBP)", "Number", precision=10, scale=2, required=True,
              integration_readable=True),
        Field("Returnable__c", "Returnable", "Checkbox", default_value="true", integration_readable=True,
              description="False for items the shared return policy never accepts."),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("Item", ("Name", "Order__c", "Item_Key__c", "Line_Number__c"),
                      ("Category__c", "Quantity__c", "Unit_Price_GBP__c", "Returnable__c")),
    ),
)

DELIVERY = SObject(
    name="ShopEasy_Delivery__c", label="ShopEasy Delivery", plural_label="ShopEasy Deliveries",
    description="Delivery tracking for one ShopEasy order.",
    name_label="Delivery", seed_key="Delivery_Key__c", sharing_model="ControlledByParent",
    enable_history=True, integration_readable=True,
    fields=(
        Field("Order__c", "Order", "MasterDetail", reference_to="ShopEasy_Order__c", required=True,
              relationship_name="Deliveries", relationship_label="Deliveries", integration_readable=True),
        _ext_id("Delivery_Key__c", "Delivery Key", 20, "One delivery per order; the order number.",
                required=True, integration=True),
        Field("Status__c", "Delivery Status", "Picklist", values=DELIVERY_STATUSES, required=True,
              integration_readable=True),
        Field("Carrier__c", "Carrier", "Text", length=80, integration_readable=True),
        Field("Tracking_Number__c", "Tracking Number", "Text", length=40, integration_readable=True),
        Field("Shipped_Date__c", "Shipped Date", "Date", integration_readable=True),
        Field("Expected_Date__c", "Expected Date", "Date", integration_readable=True,
              description="The date ShopEasy currently promises."),
        Field("Original_Expected_Date__c", "Original Expected Date", "Date", integration_readable=True,
              description="The first promised date; differs from Expected Date when late."),
        Field("Delivered_Date__c", "Delivered Date", "Date", integration_readable=True),
        Field("Last_Location__c", "Last Location", "Text", length=255, integration_readable=True),
        Field("Last_Update_Date__c", "Last Tracking Update", "Date", integration_readable=True),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("Delivery", ("Name", "Order__c", "Delivery_Key__c", "Status__c", "Carrier__c",
                                   "Tracking_Number__c"),
                      ("Shipped_Date__c", "Expected_Date__c", "Original_Expected_Date__c",
                       "Delivered_Date__c", "Last_Location__c", "Last_Update_Date__c")),
    ),
)

# --- Complaints ------------------------------------------------------------------

CASE = SObject(
    name="Case", label="Case", plural_label="Cases", custom=False,
    description="A distinct customer complaint with NABD analysis and lifecycle fields.",
    seed_key="NABD_Issue_Id__c",
    standard_layout="Case-Case Layout",
    integration_readable=True,
    fields=(
        _std("CaseNumber", "Case Number", "AutoNumber", permissionable=False, integration=True,
             staff_editable=False),
        _std("Subject", "Subject", "Text", integration=True),
        _std("Description", "Description", "LongTextArea"),
        _std("Status", "Status", "Picklist", permissionable=False, integration=True, values=CASE_STATUSES),
        _std("Priority", "Priority", "Picklist", integration=True, values=CASE_PRIORITIES),
        _std("Origin", "Case Origin", "Picklist", integration=True, values=CASE_ORIGINS),
        _std("AccountId", "Account", "Lookup", reference_to="Account", integration=True),
        _std("ContactId", "Contact", "Lookup", reference_to="Contact", integration=True),
        _std("OwnerId", "Owner", "Lookup", permissionable=False),
        _std("IsClosed", "Closed", "Checkbox", permissionable=False, integration=True, staff_editable=False),
        _std("ClosedDate", "Closed Date", "DateTime", integration=True, staff_editable=False),
        _std("CreatedDate", "Created Date", "DateTime", permissionable=False, integration=True,
             staff_editable=False),
        _ext_id("NABD_Issue_Id__c", "NABD Issue ID", 64,
                "Stable reference of the distinct issue this case represents.", case_sensitive=True,
                integration=True),
        Field("ShopEasy_Order__c", "ShopEasy Order", "Lookup", reference_to="ShopEasy_Order__c",
              relationship_name="Cases", relationship_label="Cases", delete_constraint="SetNull",
              integration_readable=True),
        Field("Complaint_Category__c", "Complaint Category", "Picklist", values=COMPLAINT_CATEGORIES,
              integration_readable=True,
              description="AI-assessed department category; ambiguous cases use General Support."),
        Field("Customer_Language__c", "Customer Language", "Picklist", values=LANGUAGES),
        Field("Sentiment__c", "Sentiment", "Picklist", values=SENTIMENTS,
              description="Internal assessment of the customer's feeling. Never shown to customers."),
        Field("Urgency__c", "Urgency", "Picklist", values=URGENCIES,
              description="Internal impact/urgency evidence; separate from sentiment and policy priority."),
        Field("Classification_Confidence__c", "Classification Confidence", "Number", precision=4, scale=3,
              description="0 to 1. Low values keep classification uncertainty visible to staff."),
        Field("Needs_Review__c", "Needs Staff Review", "Checkbox", default_value="false",
              description="Set when category or matching is ambiguous."),
        Field("English_Summary__c", "English Summary", "LongTextArea", length=32768, visible_lines=6,
              description="Staff-facing English summary. Original customer wording stays in evidence."),
        Field("Resolution_Outcome__c", "Resolution Outcome", "Picklist", values=RESOLUTION_OUTCOMES),
        Field("Resolution_Confirmed_At__c", "Resolution Confirmed At", "DateTime",
              description="When the customer confirmed the resolution."),
        Field("Resolution_Confirmation_Message__c", "Resolution Confirmation Message ID", "Text", length=64,
              description="Source message ID of the customer's confirmation."),
        Field("Reopen_Count__c", "Reopen Count", "Number", precision=4, scale=0, default_value="0"),
        Field("Last_Reopened_At__c", "Last Reopened At", "DateTime"),
        Field("NABD_Conversation_Id__c", "NABD Conversation ID", "Text", length=64, staff_editable=False),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection(
            "NABD Complaint Analysis",
            ("NABD_Issue_Id__c", "ShopEasy_Order__c", "Complaint_Category__c", "Customer_Language__c",
             "Needs_Review__c", "Classification_Confidence__c"),
            ("Sentiment__c", "Urgency__c", "Reopen_Count__c", "Last_Reopened_At__c", "NABD_Conversation_Id__c"),
        ),
        LayoutSection("NABD Summary", ("English_Summary__c",)),
        LayoutSection(
            "NABD Resolution",
            ("Resolution_Outcome__c", "Resolution_Confirmed_At__c"),
            ("Resolution_Confirmation_Message__c",),
        ),
    ),
    related_lists=(
        RelatedList("Complaint_Evidence__c", "Case__c",
                    ("NAME", "Evidence_Kind__c", "Original_Language__c", "Source_Message_Id__c",
                     "Captured_At__c")),
    ),
)

EVIDENCE = SObject(
    name="Complaint_Evidence__c", label="Complaint Evidence", plural_label="Complaint Evidence",
    description="Original customer wording or a verified fact supporting one case. Never translated in place.",
    name_label="Evidence", name_type="AutoNumber", name_format="EV-{000000}", seed_key="Evidence_Key__c",
    sharing_model="ControlledByParent",
    fields=(
        Field("Case__c", "Case", "MasterDetail", reference_to="Case", required=True,
              relationship_name="Complaint_Evidence", relationship_label="Complaint Evidence"),
        _ext_id("Evidence_Key__c", "Evidence Key", 140,
                "Issue ID plus source message/fact ID; one message can support several issues.",
                case_sensitive=True, required=True),
        Field("Source_Message_Id__c", "Source Message ID", "Text", length=64, external_id=True,
              staff_editable=False,
              description="Stable NABD message ID (or fact reference) this evidence came from."),
        Field("Evidence_Kind__c", "Evidence Kind", "Picklist", values=EVIDENCE_KINDS, required=True,
              description="Distinguishes customer-reported claims from tool-verified facts."),
        Field("Original_Text__c", "Original Text", "LongTextArea", length=32768, visible_lines=6,
              staff_editable=False,
              description="Exact original customer wording or the verified fact statement."),
        Field("Original_Language__c", "Original Language", "Picklist", values=LANGUAGES),
        Field("Original_Script__c", "Original Script", "Picklist", values=SCRIPTS),
        Field("Verified_Source__c", "Verified Source", "Text", length=255,
              description="Salesforce record/version or tool receipt behind a verified fact."),
        Field("Captured_At__c", "Captured At", "DateTime"),
        Field("NABD_Conversation_Id__c", "NABD Conversation ID", "Text", length=64, staff_editable=False),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("Evidence", ("Name", "Case__c", "Evidence_Kind__c", "Evidence_Key__c",
                                   "Source_Message_Id__c"),
                      ("Original_Language__c", "Original_Script__c", "Captured_At__c", "Verified_Source__c",
                       "NABD_Conversation_Id__c")),
        LayoutSection("Original Wording", ("Original_Text__c",)),
    ),
)

TASK = SObject(
    name="Task", metadata_name="Activity", label="Task", plural_label="Tasks", custom=False,
    description="Staff follow-up linked to a case or risk episode.",
    seed_key="NABD_Task_Key__c",
    fields=(
        _std("Subject", "Subject", "Text", permissionable=False),
        _std("Description", "Comments", "LongTextArea"),
        _std("Status", "Status", "Picklist", permissionable=False),
        _std("Priority", "Priority", "Picklist", permissionable=False),
        _std("ActivityDate", "Due Date", "Date"),
        _std("WhatId", "Related To", "Lookup"),
        _std("WhoId", "Name", "Lookup"),
        _std("OwnerId", "Assigned To", "Lookup", permissionable=False),
        Field("NABD_Task_Key__c", "NABD Task Key", "Text", length=80, staff_editable=False,
              description="Stable key so seeding and later warning updates never duplicate a task."),
        Field("NABD_Follow_Up_Type__c", "NABD Follow-up Type", "Picklist",
              values=_values(("case_follow_up", "Case follow-up"), ("risk_warning", "Risk warning"))),
        _fingerprint(),
    ),
)

# --- Knowledge ------------------------------------------------------------------

KNOWLEDGE = SObject(
    name="KnowledgeEntry__c", label="Knowledge Entry", plural_label="Knowledge Entries",
    description="Versioned ShopEasy FAQ and policy revisions maintained by staff in Salesforce.",
    name_label="Title", seed_key="Revision_Key__c", sharing_model="Private", enable_history=True,
    tab_motif="Custom55: Books", integration_readable=True,
    fields=(
        Field("Entry_Key__c", "Entry Key", "Text", length=80, required=True, external_id=True,
              integration_readable=True,
              description="Stable source ID shared by every revision, such as policy.return_window."),
        Field("Revision__c", "Revision", "Number", precision=4, scale=0, required=True,
              integration_readable=True),
        _ext_id("Revision_Key__c", "Revision Key", 100,
                "Entry key and revision, such as policy.return_window@2. Set automatically.",
                integration=True),
        Field("Entry_Type__c", "Entry Type", "Picklist", values=KNOWLEDGE_TYPES, required=True,
              integration_readable=True),
        Field("Status__c", "Status", "Picklist", values=KNOWLEDGE_STATUSES, required=True,
              integration_readable=True,
              description="Publishing a revision retires the previously published revision of the entry."),
        Field("Language__c", "Language", "Picklist", values=_values(("en", "English"), default="en"),
              required=True, integration_readable=True),
        Field("Topic__c", "Topic", "Text", length=80, integration_readable=True),
        Field("Question__c", "Question", "Text", length=255, integration_readable=True),
        Field("Body__c", "Body", "LongTextArea", length=32768, visible_lines=8, integration_readable=True),
        Field("Search_Keywords__c", "Search Keywords", "Text", length=255, integration_readable=True),
        Field("Published_At__c", "Published At", "DateTime", integration_readable=True),
        Field("Retired_At__c", "Retired At", "DateTime"),
        _fingerprint(),
    ),
    validation_rules=(
        ValidationRule(
            "Published_Revision_Needs_Body",
            'AND(ISPICKVAL(Status__c, "Published"), ISBLANK(Body__c))',
            "A published revision needs body text.",
            "Customers may only receive published revisions with content.",
            error_field="Body__c",
        ),
        ValidationRule(
            "Revision_Must_Be_Positive",
            "Revision__c < 1",
            "Revision numbers start at 1.",
            "Keeps revision keys stable and ordered.",
            error_field="Revision__c",
        ),
    ),
    layout_sections=(
        LayoutSection("Entry", ("Name", "Entry_Key__c", "Revision__c", "Revision_Key__c", "Entry_Type__c"),
                      ("Status__c", "Language__c", "Topic__c", "Published_At__c", "Retired_At__c")),
        LayoutSection("Content", ("Question__c", "Body__c", "Search_Keywords__c")),
    ),
)

# --- Risk warnings (data structure only; the rule engine is Phase 6) --------------

RISK_RULE = SObject(
    name="Risk_Rule__c", label="Risk Rule", plural_label="Risk Rules",
    description="Versioned, explainable early-warning rule definition.",
    name_label="Rule Name", seed_key="Rule_Key__c", sharing_model="Read", tab_motif="Custom90: Radar",
    fields=(
        _ext_id("Rule_Key__c", "Rule Key", 80, "Rule code and version, such as repeat_complaints@1.",
                required=True),
        Field("Rule_Code__c", "Rule Code", "Text", length=60, required=True),
        Field("Version__c", "Version", "Number", precision=4, scale=0, required=True),
        Field("Active__c", "Active", "Checkbox", default_value="false"),
        Field("Description__c", "Description", "LongTextArea", length=4000, visible_lines=4),
        Field("Parameters_JSON__c", "Parameters (JSON)", "LongTextArea", length=4000, visible_lines=4),
        _fingerprint(),
    ),
    layout_sections=(
        LayoutSection("Rule", ("Name", "Rule_Key__c", "Rule_Code__c", "Version__c", "Active__c"),),
        LayoutSection("Definition", ("Description__c", "Parameters_JSON__c")),
    ),
)

RISK_EPISODE = SObject(
    name="Risk_Episode__c", label="Risk Episode", plural_label="Risk Episodes",
    description="One active warning for a customer and rule; owns at most one follow-up task.",
    name_label="Episode", name_type="AutoNumber", name_format="RE-{00000}", seed_key="Episode_Key__c",
    sharing_model="Private", enable_activities=True, enable_history=True, tab_motif="Custom90: Radar",
    fields=(
        _ext_id("Episode_Key__c", "Episode Key", 140, "Customer, rule key and episode start.",
                case_sensitive=True, required=True),
        Field("Account__c", "Customer Account", "Lookup", reference_to="Account", required=True,
              relationship_name="Risk_Episodes", relationship_label="Risk Episodes",
              delete_constraint="Restrict"),
        Field("Risk_Rule__c", "Risk Rule", "Lookup", reference_to="Risk_Rule__c", required=True,
              relationship_name="Episodes", relationship_label="Episodes", delete_constraint="Restrict"),
        Field("Status__c", "Status", "Picklist", values=_values(("active", "Active"), ("cleared", "Cleared"),
                                                               default="active"), required=True),
        Field("Opened_At__c", "Opened At", "DateTime"),
        Field("Cleared_At__c", "Cleared At", "DateTime"),
        Field("Last_Evaluated_At__c", "Last Evaluated At", "DateTime"),
        Field("Explanation__c", "Explanation", "LongTextArea", length=4000, visible_lines=4),
        Field("Follow_Up_Task_Id__c", "Follow-up Task ID", "Text", length=18, staff_editable=False,
              description="The one Task linked to this episode (tasks cannot be lookup targets)."),
    ),
    layout_sections=(
        LayoutSection("Episode", ("Name", "Episode_Key__c", "Account__c", "Risk_Rule__c", "Status__c"),
                      ("Opened_At__c", "Cleared_At__c", "Last_Evaluated_At__c", "Follow_Up_Task_Id__c")),
        LayoutSection("Explanation", ("Explanation__c",)),
    ),
    related_lists=(
        RelatedList("Risk_Evidence__c", "Risk_Episode__c", ("NAME", "Case__c", "Detail__c", "Observed_At__c")),
    ),
)

RISK_EVIDENCE = SObject(
    name="Risk_Evidence__c", label="Risk Evidence", plural_label="Risk Evidence",
    description="A case or complaint evidence item that supports a risk episode.",
    name_label="Risk Evidence", name_type="AutoNumber", name_format="RV-{000000}", seed_key="Evidence_Key__c",
    sharing_model="ControlledByParent",
    fields=(
        Field("Risk_Episode__c", "Risk Episode", "MasterDetail", reference_to="Risk_Episode__c", required=True,
              relationship_name="Risk_Evidence", relationship_label="Risk Evidence"),
        _ext_id("Evidence_Key__c", "Evidence Key", 160, "Episode key plus supporting record.",
                case_sensitive=True, required=True),
        Field("Case__c", "Case", "Lookup", reference_to="Case", relationship_name="Risk_Evidence",
              relationship_label="Risk Evidence", delete_constraint="SetNull"),
        Field("Complaint_Evidence__c", "Complaint Evidence", "Lookup", reference_to="Complaint_Evidence__c",
              relationship_name="Risk_Evidence", relationship_label="Risk Evidence", delete_constraint="SetNull"),
        Field("Detail__c", "Detail", "Text", length=255),
        Field("Observed_At__c", "Observed At", "DateTime"),
    ),
    layout_sections=(
        LayoutSection("Evidence", ("Name", "Risk_Episode__c", "Evidence_Key__c"),
                      ("Case__c", "Complaint_Evidence__c", "Observed_At__c")),
        LayoutSection("Detail", ("Detail__c",)),
    ),
)

# --- Operation receipts (the Phase 3 write gateway records these) -----------------

OPERATION_RECEIPT = SObject(
    name="NABD_Operation_Receipt__c", label="NABD Operation Receipt", plural_label="NABD Operation Receipts",
    description="Unique record of one NABD business operation and its confirmed result.",
    name_label="Receipt", name_type="AutoNumber", name_format="OP-{0000000}", seed_key="Operation_Id__c",
    sharing_model="Private", tab_motif="Custom62: Stamp",
    staff_access="read",
    fields=(
        _ext_id("Operation_Id__c", "Operation ID", 64, "Durable NABD operation ID; unique per side effect.",
                case_sensitive=True, required=True),
        Field("Operation_Type__c", "Operation Type", "Picklist", required=True, values=_values(
            ("create_case", "Create case"), ("update_case", "Update case"), ("reopen_case", "Reopen case"),
            ("request_closure", "Request closure"), ("close_case", "Close case"),
            ("add_evidence", "Add evidence"), ("create_task", "Create task"),
        )),
        Field("Status__c", "Result", "Picklist", required=True, values=_values(
            ("confirmed", "Confirmed"), ("failed", "Failed"), ("conflict", "Conflict"))),
        Field("Case__c", "Case", "Lookup", reference_to="Case", relationship_name="NABD_Operation_Receipts",
              relationship_label="NABD Operation Receipts", delete_constraint="SetNull"),
        Field("Target_Record_Id__c", "Target Record ID", "Text", length=18),
        Field("Expected_Version__c", "Expected Version", "Text", length=40),
        Field("Result_Version__c", "Result Version", "Text", length=40),
        Field("Request_Fingerprint__c", "Request Fingerprint", "Text", length=64),
        Field("Result_JSON__c", "Result (JSON)", "LongTextArea", length=32768, visible_lines=6),
        Field("Completed_At__c", "Completed At", "DateTime"),
    ),
    layout_sections=(
        LayoutSection("Operation", ("Name", "Operation_Id__c", "Operation_Type__c", "Status__c"),
                      ("Case__c", "Target_Record_Id__c", "Completed_At__c")),
        LayoutSection("Versions and result", ("Expected_Version__c", "Result_Version__c",
                                              "Request_Fingerprint__c", "Result_JSON__c")),
    ),
)

OBJECTS: tuple[SObject, ...] = (
    ACCOUNT, CONTACT, ORDER, ORDER_ITEM, DELIVERY, CASE, EVIDENCE, TASK, KNOWLEDGE,
    RISK_RULE, RISK_EPISODE, RISK_EVIDENCE, OPERATION_RECEIPT,
)
OBJECTS_BY_NAME = {item.name: item for item in OBJECTS}


def sobject(name: str) -> SObject:
    try:
        return OBJECTS_BY_NAME[name]
    except KeyError:
        raise KeyError(f"{name} is not in the NABD schema contract") from None


# --- Queues, groups and routing -------------------------------------------------


@dataclass(frozen=True)
class Queue:
    developer_name: str
    label: str
    category: str  # Complaint_Category__c API value routed here


QUEUES = (
    Queue("NABD_Delivery", "Delivery", "Delivery"),
    Queue("NABD_Orders_Products", "Orders & Products", "Orders_Products"),
    Queue("NABD_Billing_Payments", "Billing & Payments", "Billing_Payments"),
    Queue("NABD_Returns_Refunds", "Returns & Refunds", "Returns_Refunds"),
    Queue("NABD_General_Support", "General Support", "General_Support"),
)
QUEUE_FOR_CATEGORY = {queue.category: queue for queue in QUEUES}
REVIEW_QUEUE = QUEUE_FOR_CATEGORY["General_Support"]

STAFF_GROUP = "NABD_Staff"
INTEGRATION_GROUP = "NABD_Integration"
GROUPS = {STAFF_GROUP: "NABD Staff", INTEGRATION_GROUP: "NABD Integration"}

STAFF_PERMISSION_SET = "NABD_Staff"
INTEGRATION_PERMISSION_SET = "NABD_Integration_Read"
INTEGRATION_PERMISSION_SET_LICENSE = "SalesforceAPIIntegrationPsl"
INTEGRATION_PROFILE = "Minimum Access - API Only Integrations"

APP_NAME = "NABD_Support"
CATEGORY_ROUTE_TYPE = "NABD_Category_Route__mdt"

EXTERNAL_CLIENT_APP = "NABD_Backend"
# sfdx-project.json replaces this token from the environment when the
# integration package is deployed, so org-specific usernames stay out of Git.
RUN_AS_PLACEHOLDER = "__NABD_SALESFORCE_INTEGRATION_USERNAME__"
RUN_AS_ENVIRONMENT = "NABD_SALESFORCE_INTEGRATION_USERNAME"
