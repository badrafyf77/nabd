"""Render the deployable SFDX metadata from the schema contract.

``render()`` returns ``{relative path: file text}`` for every generated file.
``write()`` replaces the generated directories with that output; ``check()``
reports drift so tests fail when someone edits generated XML by hand.
Hand-written source (Apex, the External Client App) lives in other folders
and is never touched here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

from nabd.salesforce import schema as s

REPO_ROOT = Path(__file__).resolve().parents[4]
SALESFORCE_ROOT = REPO_ROOT / "salesforce"
SOURCE_ROOT = SALESFORCE_ROOT / "force-app" / "main" / "default"
BASE_LAYOUTS = SALESFORCE_ROOT / "base-layouts"
NS = "http://soap.sforce.com/2006/04/metadata"

# Folders whose entire content is generated. Everything else is hand-written.
GENERATED_FOLDERS = (
    "applications", "customMetadata", "groups", "layouts", "objects", "permissionsets", "queues",
    "sharingRules", "standardValueSets", "tabs",
)


@dataclass
class Node:
    """A tiny XML tree that serializes in Salesforce's own source format."""

    tag: str
    text: str | None = None
    children: list[Node] | None = None
    attrs: dict[str, str] | None = None

    def render(self, depth: int = 0) -> list[str]:
        pad = "    " * depth
        attrs = "".join(f' {key}="{_escape(value)}"' for key, value in (self.attrs or {}).items())
        if self.children:
            lines = [f"{pad}<{self.tag}{attrs}>"]
            for child in self.children:
                lines.extend(child.render(depth + 1))
            lines.append(f"{pad}</{self.tag}>")
            return lines
        if self.text is None:
            return [f"{pad}<{self.tag}{attrs}/>"]
        return [f"{pad}<{self.tag}{attrs}>{_escape(self.text)}</{self.tag}>"]


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def n(tag: str, value: object = None, *children: Node | None) -> Node:
    if isinstance(value, Node):
        children = (value, *children)
        value = None
    kept = [child for child in children if child is not None]
    if kept:
        return Node(tag, children=kept)
    if isinstance(value, bool):
        value = "true" if value else "false"
    return Node(tag, None if value is None else str(value))


def document(root: str, *children: Node | None, attrs: dict[str, str] | None = None) -> str:
    top = Node(root, children=[child for child in children if child is not None],
               attrs={"xmlns": NS, **(attrs or {})})
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(top.render()) + "\n"


# --- Objects and fields ----------------------------------------------------------


def _external_sharing(model: str) -> str:
    return "ControlledByParent" if model == "ControlledByParent" else "Private"


def object_file(obj: s.SObject) -> str:
    name_field = n(
        "nameField",
        n("displayFormat", obj.name_format) if obj.name_type == "AutoNumber" else None,
        n("label", obj.name_label),
        n("trackHistory", False),
        n("type", obj.name_type),
    )
    return document(
        "CustomObject",
        n("deploymentStatus", "Deployed"),
        n("description", obj.description),
        n("enableActivities", obj.enable_activities),
        n("enableBulkApi", True),
        n("enableFeeds", False),
        n("enableHistory", obj.enable_history),
        n("enableLicensing", False),
        n("enableReports", True),
        n("enableSearch", True),
        n("enableSharing", True),
        n("enableStreamingApi", True),
        n("externalSharingModel", _external_sharing(obj.sharing_model)),
        n("label", obj.label),
        name_field,
        n("pluralLabel", obj.plural_label),
        n("sharingModel", obj.sharing_model),
        n("visibility", "Public"),
    )


def _value_set(values: Iterable[s.PicklistValue]) -> Node:
    return n(
        "valueSet",
        n("restricted", True),
        n("valueSetDefinition", n("sorted", False), *[
            n("value", n("fullName", value.api), n("default", value.default), n("label", value.label))
            for value in values
        ]),
    )


def field_file(obj: s.SObject, field: s.Field) -> str:
    track = obj.custom and obj.enable_history and field.name == "Status__c"
    is_text = field.type == "Text"
    is_md = field.type == "MasterDetail"
    return document(
        "CustomField",
        n("fullName", field.name),
        n("caseSensitive", field.case_sensitive) if is_text and field.unique else None,
        n("defaultValue", field.default_value) if field.default_value is not None else (
            n("defaultValue", False) if field.type == "Checkbox" else None),
        n("deleteConstraint", field.delete_constraint) if field.type == "Lookup" else None,
        n("description", field.description) if field.description else None,
        n("externalId", field.external_id) if field.type in ("Text", "Number", "Email") else None,
        n("label", field.label),
        n("length", field.length) if field.type in ("Text", "LongTextArea") else None,
        n("precision", field.precision) if field.type == "Number" else None,
        n("referenceTo", field.reference_to) if field.is_relationship else None,
        n("relationshipLabel", field.relationship_label) if field.is_relationship else None,
        n("relationshipName", field.relationship_name) if field.is_relationship else None,
        n("relationshipOrder", 0) if is_md else None,
        n("reparentableMasterDetail", False) if is_md else None,
        n("required", field.required) if field.type not in ("Checkbox", "MasterDetail", "LongTextArea") else None,
        n("scale", field.scale) if field.type == "Number" else None,
        n("trackHistory", track) if obj.custom else None,
        n("trackTrending", False),
        n("type", field.type),
        n("unique", field.unique) if is_text else None,
        _value_set(field.values) if field.type == "Picklist" else None,
        n("visibleLines", field.visible_lines) if field.type == "LongTextArea" else None,
        n("writeRequiresMasterRead", False) if is_md else None,
    )


def validation_rule_file(rule: s.ValidationRule) -> str:
    return document(
        "ValidationRule",
        n("fullName", rule.name),
        n("active", True),
        n("description", rule.description),
        n("errorConditionFormula", rule.formula),
        n("errorDisplayField", rule.error_field) if rule.error_field else None,
        n("errorMessage", rule.message),
    )


def _case_list_views() -> dict[str, str]:
    columns = ("CASES.CASE_NUMBER", "NAME", "CASES.SUBJECT", "CASES.STATUS", "CASES.PRIORITY",
               "Complaint_Category__c", "CASES.CREATED_DATE_DATE_ONLY")
    files = {}
    for queue in s.QUEUES:
        name = f"{queue.developer_name}_Queue"
        files[f"objects/Case/listViews/{name}.listView-meta.xml"] = document(
            "ListView",
            n("fullName", name),
            *[n("columns", column) for column in columns],
            n("filterScope", "Queue"),
            n("label", f"NABD {queue.label}"),
            n("queue", queue.developer_name),
        )
    files["objects/Case/listViews/NABD_Needs_Review.listView-meta.xml"] = document(
        "ListView",
        n("fullName", "NABD_Needs_Review"),
        *[n("columns", column) for column in columns],
        n("filterScope", "Everything"),
        n("filters", n("field", "Needs_Review__c"), n("operation", "equals"), n("value", "1")),
        n("label", "NABD Needs Review"),
    )
    return files


def all_list_view_file(obj: s.SObject) -> str:
    """An "All" list view, so a custom object tab does not open on an empty Recently Viewed list."""
    return document(
        "ListView",
        n("fullName", "All"),
        *[n("columns", column) for column in obj.list_columns],
        n("filterScope", "Everything"),
        n("label", "All"),
    )


def _category_route_type() -> dict[str, str]:
    base = f"objects/{s.CATEGORY_ROUTE_TYPE}"
    files = {
        f"{base}/{s.CATEGORY_ROUTE_TYPE}.object-meta.xml": document(
            "CustomObject",
            n("description", "Maps NABD complaint categories to their department case queues."),
            n("label", "NABD Category Route"),
            n("pluralLabel", "NABD Category Routes"),
            n("visibility", "Public"),
        ),
    }
    for name, label, length in (("Category__c", "Category", 40), ("Category_Label__c", "Category Label", 80),
                                ("Queue_Developer_Name__c", "Queue Developer Name", 80)):
        files[f"{base}/fields/{name}.field-meta.xml"] = document(
            "CustomField",
            n("fullName", name),
            n("externalId", False),
            n("fieldManageability", "DeveloperControlled"),
            n("label", label),
            n("length", length),
            n("required", True),
            n("type", "Text"),
            n("unique", False),
        )
    labels = {value.api: value.label for value in s.COMPLAINT_CATEGORIES}
    xsi = {"xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
           "xmlns:xsd": "http://www.w3.org/2001/XMLSchema"}
    for queue in s.QUEUES:
        record = s.CATEGORY_ROUTE_TYPE.removesuffix("__mdt")
        values = []
        for field, value in (("Category_Label__c", labels[queue.category]), ("Category__c", queue.category),
                             ("Queue_Developer_Name__c", queue.developer_name)):
            values.append(n("values", n("field", field),
                            Node("value", value, attrs={"xsi:type": "xsd:string"})))
        files[f"customMetadata/{record}.{queue.category}.md-meta.xml"] = document(
            "CustomMetadata", n("label", labels[queue.category]), n("protected", False), *values, attrs=xsi,
        )
    return files


# --- Layouts ----------------------------------------------------------------------


def _behavior(obj: s.SObject, name: str) -> str:
    if name == "Name":
        return "Readonly" if obj.name_type == "AutoNumber" else "Required"
    field = obj.field(name)
    if field.required or field.type == "MasterDetail":
        return "Required"
    if not field.staff_editable:
        return "Readonly"
    return "Edit"


def _section(obj: s.SObject, section: s.LayoutSection, *, custom_label: bool = True) -> Node:
    columns = [section.left] + ([section.right] if section.right else [])
    return n(
        "layoutSections",
        n("customLabel", custom_label),
        n("detailHeading", True),
        n("editHeading", True),
        n("label", section.label),
        *[
            n("layoutColumns", *[
                n("layoutItems", n("behavior", _behavior(obj, name)), n("field", name)) for name in column
            ]) if column else n("layoutColumns")
            for column in columns
        ],
        n("style", "TwoColumnsTopToBottom" if section.right else "OneColumn"),
    )


def _related_list(related: s.RelatedList) -> Node:
    return n("relatedLists", *[n("fields", column) for column in related.columns],
             n("relatedList", f"{related.child}.{related.field}"))


def layout_name(obj: s.SObject) -> str:
    return obj.standard_layout or f"{obj.name}-{obj.label} Layout"


def custom_layout_file(obj: s.SObject) -> str:
    system = [n("layoutItems", n("behavior", "Readonly"), n("field", "CreatedById")),
              n("layoutItems", n("behavior", "Readonly"), n("field", "LastModifiedById"))]
    owner = [] if obj.sharing_model == "ControlledByParent" else [
        n("layoutItems", n("behavior", "Edit"), n("field", "OwnerId"))]
    return document(
        "Layout",
        *[_section(obj, section) for section in obj.layout_sections],
        n("layoutSections",
          n("customLabel", False), n("detailHeading", True), n("editHeading", False),
          n("label", "System Information"),
          n("layoutColumns", *system), n("layoutColumns", *owner) if owner else n("layoutColumns"),
          n("style", "TwoColumnsTopToBottom")),
        *[_related_list(related) for related in obj.related_lists],
        n("showEmailCheckbox", False),
        n("showHighlightsPanel", False),
        n("showInteractionLogPanel", False),
        n("showRunAssignmentRulesCheckbox", False),
        n("showSubmitAndAttachButton", False),
    )


def _from_element(element: ET.Element) -> Node:
    tag = element.tag.split("}", 1)[-1]
    children = [_from_element(child) for child in element]
    if children:
        return Node(tag, children=children)
    return Node(tag, element.text if element.text is not None else None)


def standard_layout_file(obj: s.SObject, base_text: str) -> str:
    """Add NABD sections and related lists to a retrieved standard layout."""
    root = _from_element(ET.fromstring(base_text))
    children = list(root.children or [])
    sections = [i for i, child in enumerate(children) if child.tag == "layoutSections"]
    insert_at = sections[0] + 1 if sections else 0
    added = [_section(obj, section) for section in obj.layout_sections]
    children[insert_at:insert_at] = added
    related = [i for i, child in enumerate(children) if child.tag == "relatedLists"]
    if obj.related_lists:
        position = (related[-1] + 1) if related else len(children)
        children[position:position] = [_related_list(item) for item in obj.related_lists]
    return document("Layout", *children)


# --- Access, navigation and routing -------------------------------------------------


def _field_permissions(objects: Iterable[s.SObject], *, integration: bool) -> list[Node]:
    entries: dict[str, bool] = {}
    for obj in objects:
        for field in obj.fields:
            if not field.grants_field_permission:
                continue
            if integration:
                if not (obj.integration_readable and field.integration_readable):
                    continue
                editable = False
            else:
                if not field.custom:
                    continue
                editable = field.staff_editable and obj.staff_access == "edit"
            # Activity fields are permissioned per activity type; "Activity.X" does not persist.
            owners = ("Event", "Task") if obj.folder == "Activity" else (obj.folder,)
            for owner in owners:
                entries[f"{owner}.{field.permission_field or field.name}"] = editable
    return [n("fieldPermissions", n("editable", editable), n("field", name), n("readable", True))
            for name, editable in sorted(entries.items())]


def _object_permission(name: str, *, create: bool, edit: bool, view_all: bool) -> Node:
    return n(
        "objectPermissions",
        n("allowCreate", create),
        n("allowDelete", False),
        n("allowEdit", edit),
        n("allowRead", True),
        n("modifyAllRecords", False),
        n("object", name),
        n("viewAllRecords", view_all),
    )


def _staff_view_all(obj: s.SObject) -> bool:
    """Custom objects get View All; children inherit their master's setting."""
    if obj.master:
        return _staff_view_all(s.sobject(obj.master))
    return obj.custom


def staff_permission_set() -> str:
    objects = [obj for obj in s.OBJECTS if obj.name != "Task"]
    permissions = []
    for obj in sorted(objects, key=lambda item: item.name):
        edit = obj.staff_access == "edit"
        permissions.append(_object_permission(obj.name, create=edit, edit=edit, view_all=_staff_view_all(obj)))
    tabs = sorted(obj.name for obj in s.OBJECTS if obj.tab_motif)
    return document(
        "PermissionSet",
        n("applicationVisibilities", n("application", s.APP_NAME), n("visible", True)),
        n("customMetadataTypeAccesses", n("enabled", True), n("name", s.CATEGORY_ROUTE_TYPE)),
        n("description", "Staff access to NABD customers, orders, cases, evidence, knowledge, warnings "
                         "and receipts. Staff edit knowledge and cases; receipts are read-only."),
        *_field_permissions(s.OBJECTS, integration=False),
        n("hasActivationRequired", False),
        n("label", "NABD Staff"),
        *permissions,
        *[n("tabSettings", n("tab", tab), n("visibility", "Visible")) for tab in tabs],
    )


def integration_permission_set() -> str:
    readable = sorted((obj for obj in s.OBJECTS if obj.integration_readable), key=lambda item: item.name)
    return document(
        "PermissionSet",
        n("description", "Read-only API access for the NABD backend integration user. Covers only the "
                         "fields the read adapter returns. Writes go only through the case command gateway."),
        *_field_permissions(readable, integration=True),
        n("hasActivationRequired", False),
        n("label", "NABD Integration Read"),
        *[_object_permission(obj.name, create=False, edit=False, view_all=False) for obj in readable],
    )


def gateway_permission_set() -> str:
    """Execute access to the typed case command gateway, and nothing else.

    The gateway class runs in system mode and enforces customer ownership,
    versions, transitions and receipts itself, so the integration user needs no
    create or edit permission on any object.
    """
    return document(
        "PermissionSet",
        n("classAccesses", n("apexClass", s.GATEWAY_CLASS), n("enabled", True)),
        n("description", "Lets the NABD integration user call the NabdCaseCommands case command gateway. "
                         "Grants no object create or edit rights; direct record writes stay refused."),
        n("hasActivationRequired", False),
        n("label", "NABD Integration Gateway"),
    )


def app_file() -> str:
    tabs = ["standard-Case", "standard-Account", "standard-Contact", "ShopEasy_Order__c", "KnowledgeEntry__c",
            "Risk_Episode__c", "Risk_Rule__c", "NABD_Operation_Receipt__c", "standard-report",
            "standard-Dashboard"]
    return document(
        "CustomApplication",
        n("brand", n("headerColor", "#0B5CAB"), n("shouldOverrideOrgTheme", False)),
        n("description", "NABD staff workspace for ShopEasy support, complaints and knowledge."),
        n("formFactors", "Large"),
        n("isNavAutoTempTabsDisabled", False),
        n("isNavPersonalizationDisabled", False),
        n("isNavTabPersistenceDisabled", False),
        n("label", "NABD Support"),
        n("navType", "Standard"),
        *[n("tabs", tab) for tab in tabs],
        n("uiType", "Lightning"),
    )


def queue_file(queue: s.Queue) -> str:
    return document(
        "Queue",
        n("doesSendEmailToMembers", False),
        n("name", queue.label),
        n("queueMembers", n("publicGroups", n("publicGroup", s.STAFF_GROUP))),
        n("queueSobject", n("sobjectType", "Case")),
    )


def case_status_file() -> str:
    return document(
        "StandardValueSet",
        n("sorted", False),
        *[n("standardValue", n("fullName", value.api), n("default", value.default), n("label", value.label),
            n("closed", value.closed)) for value in s.CASE_STATUSES],
    )


def case_origin_file() -> str:
    return document(
        "StandardValueSet",
        n("sorted", False),
        *[n("standardValue", n("fullName", value.api), n("default", value.default), n("label", value.label))
          for value in s.CASE_ORIGINS],
    )


def knowledge_sharing_file() -> str:
    return document(
        "SharingRules",
        n("sharingCriteriaRules",
          n("fullName", "NABD_Published_Knowledge"),
          n("accessLevel", "Read"),
          n("description", "The integration user sees published revisions only; drafts and retired "
                           "revisions stay with staff."),
          n("label", "NABD published knowledge"),
          n("sharedTo", n("group", s.INTEGRATION_GROUP)),
          n("criteriaItems", n("field", "Status__c"), n("operation", "equals"), n("value", "Published")),
          n("includeRecordsOwnedByAll", True)),
    )


# --- Assembly -----------------------------------------------------------------------


def render() -> dict[str, str]:
    files: dict[str, str] = {}
    for obj in s.OBJECTS:
        base = f"objects/{obj.folder}"
        if obj.custom:
            files[f"{base}/{obj.name}.object-meta.xml"] = object_file(obj)
            files[f"layouts/{layout_name(obj)}.layout-meta.xml"] = custom_layout_file(obj)
        elif obj.standard_layout:
            base_text = (BASE_LAYOUTS / f"{obj.standard_layout}.layout-meta.xml").read_text(encoding="utf-8")
            files[f"layouts/{obj.standard_layout}.layout-meta.xml"] = standard_layout_file(obj, base_text)
        for field in obj.custom_fields:
            files[f"{base}/fields/{field.name}.field-meta.xml"] = field_file(obj, field)
        for rule in obj.validation_rules:
            files[f"{base}/validationRules/{rule.name}.validationRule-meta.xml"] = validation_rule_file(rule)
        if obj.tab_motif:
            files[f"tabs/{obj.name}.tab-meta.xml"] = document(
                "CustomTab", n("customObject", True), n("motif", obj.tab_motif))
            if not obj.list_columns:
                raise ValueError(f"{obj.name} has a tab but no list_columns for its All list view")
            files[f"{base}/listViews/All.listView-meta.xml"] = all_list_view_file(obj)
    files.update(_case_list_views())
    files.update(_category_route_type())
    files[f"permissionsets/{s.STAFF_PERMISSION_SET}.permissionset-meta.xml"] = staff_permission_set()
    files[f"permissionsets/{s.INTEGRATION_PERMISSION_SET}.permissionset-meta.xml"] = integration_permission_set()
    files[f"permissionsets/{s.GATEWAY_PERMISSION_SET}.permissionset-meta.xml"] = gateway_permission_set()
    files[f"applications/{s.APP_NAME}.app-meta.xml"] = app_file()
    for group, label in s.GROUPS.items():
        files[f"groups/{group}.group-meta.xml"] = document(
            "Group", n("doesIncludeBosses", False), n("name", label))
    for queue in s.QUEUES:
        files[f"queues/{queue.developer_name}.queue-meta.xml"] = queue_file(queue)
    files["standardValueSets/CaseStatus.standardValueSet-meta.xml"] = case_status_file()
    files["standardValueSets/CaseOrigin.standardValueSet-meta.xml"] = case_origin_file()
    files["sharingRules/KnowledgeEntry__c.sharingRules-meta.xml"] = knowledge_sharing_file()
    return dict(sorted(files.items()))


def _existing(root: Path) -> set[str]:
    found = set()
    for folder in GENERATED_FOLDERS:
        path = root / folder
        if path.exists():
            found.update(str(item.relative_to(root)) for item in path.rglob("*") if item.is_file())
    return found


def check(root: Path = SOURCE_ROOT) -> list[str]:
    """Return human-readable drift between the contract and committed metadata."""
    expected = render()
    problems = []
    for path in sorted(_existing(root) - expected.keys()):
        problems.append(f"unexpected generated file: {path}")
    for path, text in expected.items():
        target = root / path
        if not target.exists():
            problems.append(f"missing: {path}")
        elif target.read_text(encoding="utf-8") != text:
            problems.append(f"out of date: {path}")
    return problems


def write(root: Path = SOURCE_ROOT) -> int:
    expected = render()
    for path in _existing(root) - expected.keys():
        (root / path).unlink()
    for path, text in expected.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    for folder in GENERATED_FOLDERS:  # drop now-empty directories
        for directory in sorted((root / folder).rglob("*"), reverse=True) if (root / folder).exists() else []:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    return len(expected)
