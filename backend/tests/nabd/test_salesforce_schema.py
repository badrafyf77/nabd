"""The schema contract and the metadata generated from it. No Salesforce calls."""
import re
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from nabd.salesforce import metadata
from nabd.salesforce import schema as s

NS = {"m": metadata.NS}
# Internal assessments the integration user must never read. The English summary
# became readable in Phase 3 for case matching; only the Complaint stage's
# separately scoped read MCP tool returns it.
INTERNAL_CASE_FIELDS = {
    "Sentiment__c", "Urgency__c", "Classification_Confidence__c", "Needs_Review__c", "Description",
    "Customer_Language__c", "NABD_Assessed_Priority__c", "NABD_Priority_Reason__c", "NABD_Summary_Hash__c",
    s.SEED_FINGERPRINT,
}
BOOKKEEPING_FIELDS = {s.SEED_FINGERPRINT, "NABD_Summary_Hash__c"}  # not shown on layouts


def _parse(text: str) -> ET.Element:
    return ET.fromstring(text.encode())


def test_names_and_sizes_respect_salesforce_limits():
    seen_objects = set()
    for obj in s.OBJECTS:
        assert obj.name not in seen_objects
        seen_objects.add(obj.name)
        names = [field.name for field in obj.fields]
        assert len(names) == len(set(names)), obj.name
        for field in obj.custom_fields:
            assert re.fullmatch(r"[A-Z][A-Za-z0-9_]{0,39}__c", field.name), field.name
            assert "__" not in field.name[:-3], field.name
            assert len(field.label) <= 40, field.name
            if field.type == "Text":
                assert field.length and field.length <= 255, field.name
            if field.type == "LongTextArea":
                assert 256 <= field.length <= 131072 and field.visible_lines, field.name
            if field.type == "Picklist":
                assert field.values and sum(value.default for value in field.values) <= 1, field.name
            if field.unique:
                assert field.type == "Text" and field.external_id, field.name
            if field.case_sensitive:
                assert field.unique, field.name


def test_relationships_point_inside_the_contract():
    for obj in s.OBJECTS:
        for field in obj.fields:
            if field.custom and field.is_relationship:
                assert field.reference_to in s.OBJECTS_BY_NAME, field.name
                assert field.relationship_name and field.relationship_label, field.name
                if field.type == "Lookup":
                    assert field.delete_constraint, field.name
                    assert field.delete_constraint == "Restrict" or not field.required, field.name
        if obj.custom and obj.sharing_model == "ControlledByParent":
            assert obj.master is not None, obj.name


def test_every_seeded_object_has_a_stable_key():
    for obj in s.OBJECTS:
        if obj.seed_key:
            key = obj.field(obj.seed_key)
            # Activities cannot hold unique external IDs; the seed matches Task keys by query.
            assert key.unique or obj.name == "Task", obj.name


def test_queues_cover_each_category_once_and_review_goes_to_general_support():
    assert sorted(queue.category for queue in s.QUEUES) == sorted(v.api for v in s.COMPLAINT_CATEGORIES)
    assert [queue.label for queue in s.QUEUES] == [
        "Delivery", "Orders & Products", "Billing & Payments", "Returns & Refunds", "General Support"]
    assert s.REVIEW_QUEUE.category == "General_Support"


def test_case_statuses_match_the_accepted_lifecycle():
    assert [value.api for value in s.CASE_STATUSES] == [
        "New", "Working", "Waiting on Customer", "Waiting on Staff", "Closed"]
    assert [value.api for value in s.CASE_STATUSES if value.closed] == ["Closed"]
    assert [value.api for value in s.CASE_STATUSES if value.default] == ["New"]


def test_integration_user_reads_only_customer_safe_data():
    readable = {obj.name for obj in s.OBJECTS if obj.integration_readable}
    # Phase 5: public case comments (the read adapter filters IsPublished = true; CaseComment access
    # follows the case) and subscribing to the case change event, which carries identifiers only.
    assert readable == {"Account", "Contact", "ShopEasy_Order__c", "ShopEasy_Order_Item__c",
                        "ShopEasy_Delivery__c", "Case", "CaseComment", "KnowledgeEntry__c", "NABD_Case_Change__e"}
    case_fields = {field.name for field in s.CASE.fields if field.integration_readable}
    assert not case_fields & INTERNAL_CASE_FIELDS
    for obj in s.OBJECTS:
        for field in obj.fields:
            if field.name == s.SEED_FINGERPRINT:
                assert not field.integration_readable, obj.name


def test_layouts_only_reference_contract_fields_and_show_every_staff_field():
    for obj in s.OBJECTS:
        shown = {name for section in obj.layout_sections for name in (*section.left, *section.right)}
        for name in shown - {"Name"}:
            obj.field(name)
        if obj.layout_sections:
            hidden = {field.name for field in obj.custom_fields} - shown - BOOKKEEPING_FIELDS
            assert not hidden, f"{obj.name} hides {hidden}"
        for related in obj.related_lists:
            child = s.sobject(related.child)
            assert child.field(related.field).reference_to == obj.name


# The repository's salesforce/ folder is outside the Linux test image's build context.
needs_salesforce_project = pytest.mark.skipif(
    not metadata.BASE_LAYOUTS.exists(), reason="salesforce/ is not available in this test environment")


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    if not metadata.BASE_LAYOUTS.exists():
        pytest.skip("salesforce/ is not available in this test environment")
    return metadata.render()


@needs_salesforce_project
def test_generated_metadata_is_committed_and_current():
    assert metadata.check() == []


def test_rendering_is_deterministic_and_well_formed(rendered):
    assert rendered == metadata.render()
    for path, text in rendered.items():
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'), path
        _parse(text)


def test_integration_permission_set_is_read_only(rendered):
    root = _parse(rendered["permissionsets/NABD_Integration_Read.permissionset-meta.xml"])
    for permission in root.findall("m:objectPermissions", NS):
        flags = {child.tag.split("}")[1]: child.text for child in permission}
        assert flags["allowRead"] == "true"
        assert {flags[name] for name in ("allowCreate", "allowEdit", "allowDelete", "modifyAllRecords",
                                         "viewAllRecords")} == {"false"}
    fields = {item.find("m:field", NS).text for item in root.findall("m:fieldPermissions", NS)}
    assert all(item.find("m:editable", NS).text == "false" for item in root.findall("m:fieldPermissions", NS))
    assert not {f"Case.{name}" for name in INTERNAL_CASE_FIELDS} & fields
    assert "Account.BillingAddress" in fields and "Account.BillingCity" not in fields
    assert "Case.English_Summary__c" in fields  # case matching (Phase 3)


@needs_salesforce_project
def test_gateway_permission_set_only_runs_the_gateway_class(rendered):
    root = _parse(rendered[f"permissionsets/{s.GATEWAY_PERMISSION_SET}.permissionset-meta.xml"])
    classes = [(item.find("m:apexClass", NS).text, item.find("m:enabled", NS).text)
               for item in root.findall("m:classAccesses", NS)]
    assert classes == [(s.GATEWAY_CLASS, "true")]
    assert not root.findall("m:objectPermissions", NS) and not root.findall("m:fieldPermissions", NS)
    assert (Path(metadata.SOURCE_ROOT) / "classes" / f"{s.GATEWAY_CLASS}.cls").exists()


def test_permission_sets_never_list_required_or_master_detail_fields(rendered):
    always_visible = {f"{obj.folder}.{field.name}" for obj in s.OBJECTS for field in obj.fields
                      if field.required or field.type == "MasterDetail"}
    for name in (s.STAFF_PERMISSION_SET, s.INTEGRATION_PERMISSION_SET):
        root = _parse(rendered[f"permissionsets/{name}.permissionset-meta.xml"])
        listed = {item.find("m:field", NS).text for item in root.findall("m:fieldPermissions", NS)}
        assert not listed & always_visible, name


def test_activity_fields_are_permissioned_on_task_and_event(rendered):
    text = rendered["permissionsets/NABD_Staff.permissionset-meta.xml"]
    assert "<field>Activity." not in text
    assert "<field>Task.NABD_Task_Key__c</field>" in text and "<field>Event.NABD_Task_Key__c</field>" in text


def test_knowledge_sharing_exposes_published_revisions_only(rendered):
    root = _parse(rendered["sharingRules/KnowledgeEntry__c.sharingRules-meta.xml"])
    rules = root.findall("m:sharingCriteriaRules", NS)
    assert len(rules) == 1
    criteria = rules[0].findall("m:criteriaItems", NS)
    assert [(c.find("m:field", NS).text, c.find("m:operation", NS).text, c.find("m:value", NS).text)
            for c in criteria] == [("Status__c", "equals", "Published")]
    assert rules[0].find("m:sharedTo/m:group", NS).text == s.INTEGRATION_GROUP
    knowledge = _parse(rendered["objects/KnowledgeEntry__c/KnowledgeEntry__c.object-meta.xml"])
    assert knowledge.find("m:sharingModel", NS).text == "Private"


@needs_salesforce_project
def test_standard_layouts_keep_the_original_and_add_nabd_sections(rendered):
    for obj in (s.ACCOUNT, s.CONTACT, s.CASE):
        base = (metadata.BASE_LAYOUTS / f"{obj.standard_layout}.layout-meta.xml").read_text(encoding="utf-8")
        original = {item.text for item in _parse(base).iter(f"{{{metadata.NS}}}field")}
        layout = _parse(rendered[f"layouts/{obj.standard_layout}.layout-meta.xml"])
        present = {item.text for item in layout.iter(f"{{{metadata.NS}}}field")}
        expected = {name for section in obj.layout_sections for name in (*section.left, *section.right)}
        assert original <= present and expected <= present


def test_category_routes_and_queues_are_generated_from_the_contract(rendered):
    for queue in s.QUEUES:
        root = _parse(rendered[f"queues/{queue.developer_name}.queue-meta.xml"])
        assert root.find("m:queueSobject/m:sobjectType", NS).text == "Case"
        route = rendered[f"customMetadata/NABD_Category_Route.{queue.category}.md-meta.xml"]
        assert queue.developer_name in route and f">{queue.category}<" in route
