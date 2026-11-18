"""Developer-only org configuration and verification (uses the sf CLI session).

``configure`` gives the developer (staff) user the NABD staff permission set,
and creates/configures the restricted integration user that the External
Client App runs as. ``verify`` compares the live org with the schema contract
and the fixture dataset. Neither prints or stores credentials.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from nabd.salesforce import fixtures as fx
from nabd.salesforce import schema as s
from nabd.salesforce import seed
from nabd.salesforce.rest import SalesforceClient
from nabd.salesforce.soql import literal, literal_list


def integration_username(org_id: str) -> str:
    """Globally unique, org-specific username for the integration user."""
    return f"nabd-integration.{org_id.lower()}@example.com"


async def _one(client: SalesforceClient, soql: str) -> dict[str, Any] | None:
    rows = await client.query(soql)
    return rows[0] if rows else None


async def _create(client: SalesforceClient, sobject: str, values: dict[str, Any]) -> str:
    result = await client.request("POST", f"{client.base_path}/sobjects/{sobject}", json_body=values)
    return result["id"]


@dataclass
class ConfigureResult:
    staff_username: str
    integration_username: str
    changes: list[str] = field(default_factory=list)


async def configure(client: SalesforceClient, *, org_id: str, staff_username: str,
                    create_integration_user: bool) -> ConfigureResult:
    result = ConfigureResult(staff_username, integration_username(org_id))
    staff = await _one(client, "SELECT Id, Email, TimeZoneSidKey, LocaleSidKey, LanguageLocaleKey "
                               f"FROM User WHERE Username = {literal(staff_username)}")
    if staff is None:
        raise RuntimeError("The sf CLI user was not found in the org.")
    sets = {row["Name"]: row["Id"] for row in await client.query(
        "SELECT Id, Name FROM PermissionSet WHERE Name IN "
        f"{literal_list([s.STAFF_PERMISSION_SET, s.INTEGRATION_PERMISSION_SET])}")}
    groups = {row["DeveloperName"]: row["Id"] for row in await client.query(
        "SELECT Id, DeveloperName FROM Group WHERE Type = 'Regular' AND DeveloperName IN "
        f"{literal_list(list(s.GROUPS))}")}
    if len(sets) != 2 or len(groups) != len(s.GROUPS):
        raise RuntimeError("Deploy the metadata before configuring access.")

    async def ensure_permission_set(user_id: str, name: str) -> None:
        if not await _one(client, "SELECT Id FROM PermissionSetAssignment WHERE AssigneeId = "
                                  f"{literal(user_id)} AND PermissionSetId = {literal(sets[name])}"):
            await _create(client, "PermissionSetAssignment", {"AssigneeId": user_id, "PermissionSetId": sets[name]})
            result.changes.append(f"assigned {name}")

    async def ensure_group(user_id: str, name: str) -> None:
        if not await _one(client, f"SELECT Id FROM GroupMember WHERE UserOrGroupId = {literal(user_id)} "
                                  f"AND GroupId = {literal(groups[name])}"):
            await _create(client, "GroupMember", {"UserOrGroupId": user_id, "GroupId": groups[name]})
            result.changes.append(f"added to group {name}")

    await ensure_permission_set(staff["Id"], s.STAFF_PERMISSION_SET)
    await ensure_group(staff["Id"], s.STAFF_GROUP)

    integration = await _one(client, "SELECT Id, IsActive, Profile.Name FROM User WHERE Username = "
                                     f"{literal(result.integration_username)}")
    if integration is None:
        if not create_integration_user:
            raise RuntimeError("The integration user does not exist. Re-run with --create-integration-user.")
        profile = await _one(client, f"SELECT Id FROM Profile WHERE Name = {literal(s.INTEGRATION_PROFILE)}")
        if profile is None:
            raise RuntimeError(f"Profile '{s.INTEGRATION_PROFILE}' is not available in this org.")
        user_id = await _create(client, "User", {
            "Username": result.integration_username, "LastName": "NABD Integration", "FirstName": "NABD",
            "Alias": "nabdint", "Email": staff["Email"], "ProfileId": profile["Id"],
            "TimeZoneSidKey": staff["TimeZoneSidKey"], "LocaleSidKey": staff["LocaleSidKey"],
            "LanguageLocaleKey": staff["LanguageLocaleKey"], "EmailEncodingKey": "UTF-8",
        })
        result.changes.append("created integration user")
    else:
        if not integration["IsActive"] or (integration.get("Profile") or {}).get("Name") != s.INTEGRATION_PROFILE:
            raise RuntimeError("The integration user exists but is inactive or has an unexpected profile.")
        user_id = integration["Id"]

    license_row = await _one(client, "SELECT Id FROM PermissionSetLicense WHERE DeveloperName = "
                                     f"{literal(s.INTEGRATION_PERMISSION_SET_LICENSE)}")
    if license_row is None:
        raise RuntimeError("The Salesforce API Integration permission set license is not available.")
    if not await _one(client, f"SELECT Id FROM PermissionSetLicenseAssign WHERE AssigneeId = {literal(user_id)} "
                              f"AND PermissionSetLicenseId = {literal(license_row['Id'])}"):
        await _create(client, "PermissionSetLicenseAssign",
                      {"AssigneeId": user_id, "PermissionSetLicenseId": license_row["Id"]})
        result.changes.append("assigned Salesforce API Integration license")
    await ensure_permission_set(user_id, s.INTEGRATION_PERMISSION_SET)
    await ensure_group(user_id, s.INTEGRATION_GROUP)

    return result


async def approve_client_app(client: SalesforceClient) -> bool:
    """Admit the integration permission set to the admin-approved client app.

    Run after every deployment of the app's OAuth policy: deploying that policy
    clears its approved permission sets. Returns True if access was granted now.
    """
    app = await _one(client, "SELECT Id FROM ExternalClientApplication WHERE DeveloperName = "
                             f"{literal(s.EXTERNAL_CLIENT_APP)}")
    permission_set = await _one(client, "SELECT Id FROM PermissionSet WHERE Name = "
                                        f"{literal(s.INTEGRATION_PERMISSION_SET)}")
    if app is None or permission_set is None:
        raise RuntimeError(f"Deploy {s.EXTERNAL_CLIENT_APP} and {s.INTEGRATION_PERMISSION_SET} first.")
    approvals = await client.query("SELECT ParentId FROM SetupEntityAccess WHERE SetupEntityId = "
                                   f"{literal(app['Id'])}")
    if any(row["ParentId"] == permission_set["Id"] for row in approvals):
        return False
    await _create(client, "SetupEntityAccess", {"ParentId": permission_set["Id"], "SetupEntityId": app["Id"]})
    return True


# --- Verification -------------------------------------------------------------------

DESCRIBE_TYPES = {
    "Text": {"string", "combobox"}, "LongTextArea": {"textarea"}, "Number": {"double", "int"}, "Date": {"date"},
    "DateTime": {"datetime"}, "Checkbox": {"boolean"}, "Picklist": {"picklist"}, "Lookup": {"reference"},
    "MasterDetail": {"reference"}, "Email": {"email"}, "Phone": {"phone"}, "AutoNumber": {"string"},
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def _compare(name: str, expected: Any, actual: Any) -> Check:
    return Check(name, expected == actual, "" if expected == actual else f"expected {expected!r}, got {actual!r}")


async def verify_schema(client: SalesforceClient) -> list[Check]:
    checks = []
    for obj in s.OBJECTS:
        describe = await client.request("GET", f"{client.base_path}/sobjects/{obj.name}/describe")
        live = {item["name"]: item for item in describe["fields"]}
        problems = []
        for definition in obj.fields:
            actual = live.get(definition.name)
            if actual is None:
                problems.append(f"{definition.name} missing")
                continue
            if actual["type"] not in DESCRIBE_TYPES[definition.type]:
                problems.append(f"{definition.name} type {actual['type']}")
            if definition.custom:
                if bool(actual.get("externalId")) != definition.external_id:
                    problems.append(f"{definition.name} externalId")
                if bool(actual.get("unique")) != definition.unique:
                    problems.append(f"{definition.name} unique")
                if definition.is_relationship and actual.get("referenceTo") != [definition.reference_to]:
                    problems.append(f"{definition.name} referenceTo {actual.get('referenceTo')}")
            if definition.values and definition.custom:
                active = [value["value"] for value in actual.get("picklistValues", []) if value["active"]]
                if active != [value.api for value in definition.values]:
                    problems.append(f"{definition.name} values {active}")
        checks.append(Check(f"schema {obj.name}", not problems, "; ".join(problems)))
    statuses = await client.query("SELECT ApiName, IsClosed FROM CaseStatus ORDER BY SortOrder")
    checks.append(_compare("case statuses", [(v.api, v.closed) for v in s.CASE_STATUSES],
                           [(row["ApiName"], row["IsClosed"]) for row in statuses]))
    case = await client.request("GET", f"{client.base_path}/sobjects/Case/describe")
    origins = next(item for item in case["fields"] if item["name"] == "Origin")
    checks.append(Check("case origin includes NABD Chat",
                        "NABD Chat" in {value["value"] for value in origins["picklistValues"] if value["active"]}))
    queues = await client.query(
        "SELECT Queue.DeveloperName, SobjectType FROM QueueSobject WHERE Queue.DeveloperName IN "
        f"{literal_list([queue.developer_name for queue in s.QUEUES])}")
    checks.append(_compare("queues accept cases", sorted(queue.developer_name for queue in s.QUEUES),
                           sorted(row["Queue"]["DeveloperName"] for row in queues if row["SobjectType"] == "Case")))
    routes = await client.query(f"SELECT Category__c, Queue_Developer_Name__c FROM {s.CATEGORY_ROUTE_TYPE}")
    checks.append(_compare("category routes", {q.category: q.developer_name for q in s.QUEUES},
                           {row["Category__c"]: row["Queue_Developer_Name__c"] for row in routes}))
    tabbed = sorted(obj.name for obj in s.OBJECTS if obj.tab_motif)
    views = await client.query(
        f"SELECT SobjectType FROM ListView WHERE DeveloperName = 'All' AND SobjectType IN {literal_list(tabbed)}")
    checks.append(_compare("custom tabs have an All list view", tabbed, sorted(row["SobjectType"] for row in views)))
    return checks


async def verify_data(client: SalesforceClient, dataset: fx.Dataset) -> list[Check]:
    checks = []
    expected = Counter(record.sobject for record in seed.records(dataset))
    for sobject, count in expected.items():
        obj = s.sobject(sobject)
        keys = [record.key for record in seed.records(dataset) if record.sobject == sobject]
        rows = []
        for start in range(0, len(keys), 100):
            rows += await client.query(f"SELECT Id FROM {sobject} WHERE {obj.seed_key} IN "
                                       f"{literal_list(keys[start:start + 100])}")
        checks.append(_compare(f"fixture count {sobject}", count, len(rows)))
    orders = await client.query(f"SELECT Order_Number__c, Account__r.ShopEasy_Customer_Id__c, "
                                f"Contact__r.ShopEasy_Customer_Id__c, (SELECT Id FROM Items__r), "
                                f"(SELECT Id FROM Deliveries__r) FROM {s.ORDER.name}")
    by_number = {row["Order_Number__c"]: row for row in orders}
    bad = [order.order_number for order in dataset.orders
           if order.order_number not in by_number
           or by_number[order.order_number]["Account__r"]["ShopEasy_Customer_Id__c"] != order.customer_id
           or (by_number[order.order_number].get("Contact__r") or {}).get("ShopEasy_Customer_Id__c") != order.customer_id
           or len(by_number[order.order_number].get("Items__r") or []) != len(order.items)
           or len(by_number[order.order_number].get("Deliveries__r") or []) != 1]
    checks.append(Check("orders link customer, items and one delivery", not bad, ", ".join(bad)))
    duplicates = await client.query(
        f"SELECT Entry_Key__c, COUNT(Id) total FROM {s.KNOWLEDGE.name} WHERE Status__c = 'Published' "
        "GROUP BY Entry_Key__c HAVING COUNT(Id) > 1")
    checks.append(Check("one published revision per entry", not duplicates,
                        ", ".join(row["Entry_Key__c"] for row in duplicates)))
    states = Counter(row["Status__c"] for row in await client.query(
        f"SELECT Status__c FROM {s.KNOWLEDGE.name}"))
    checks.append(_compare("knowledge publication states",
                           Counter(entry.status for entry in dataset.knowledge), states))
    policy_days = await client.query(
        f"SELECT Entry_Key__c, Policy_Days__c FROM {s.KNOWLEDGE.name} "
        "WHERE Status__c = 'Published' AND Policy_Days__c != null")
    checks.append(_compare(
        "published policy days",
        {entry.entry_key: entry.policy_days for entry in dataset.knowledge
         if entry.status == "Published" and entry.policy_days is not None},
        {row["Entry_Key__c"]: int(row["Policy_Days__c"]) for row in policy_days}))
    queues = {q.developer_name: q.category for q in s.QUEUES}
    cases = await client.query(
        "SELECT Id, NABD_Issue_Id__c, OwnerId, Account.ShopEasy_Customer_Id__c, "
        "(SELECT Id FROM Complaint_Evidence__r) FROM Case WHERE NABD_Issue_Id__c != null")
    queue_ids = {row["Id"]: row["DeveloperName"] for row in await client.query(
        "SELECT Id, DeveloperName FROM Group WHERE Type = 'Queue'")}
    by_issue = {row["NABD_Issue_Id__c"]: row for row in cases}
    bad = []
    for complaint in dataset.complaints:
        row = by_issue.get(complaint.issue_id)
        if (row is None or queues.get(queue_ids.get(row["OwnerId"], "")) != complaint.category
                or row["Account"]["ShopEasy_Customer_Id__c"] != complaint.customer_id
                or len(row.get("Complaint_Evidence__r") or []) != len(complaint.evidence)):
            bad.append(complaint.issue_id)
    checks.append(Check("cases route to category queues with evidence", not bad, ", ".join(bad)))
    tasks = await client.query("SELECT NABD_Task_Key__c, WhatId FROM Task WHERE NABD_Task_Key__c != null")
    case_ids = {row["Id"]: row["NABD_Issue_Id__c"] for row in cases}
    task_cases = {row["NABD_Task_Key__c"]: case_ids.get(row["WhatId"]) for row in tasks}
    checks.append(_compare("follow-up tasks link their cases",
                           {item.task_key: item.issue_id for item in dataset.follow_ups}, task_cases))
    return checks


async def verify_access(client: SalesforceClient, *, org_id: str, staff_username: str) -> list[Check]:
    def assigned(rows: list[dict[str, Any]], name: str) -> bool:
        return any(row["PermissionSet"]["Name"] == name for row in rows)

    checks = []
    staff_sets = await client.query("SELECT PermissionSet.Name FROM PermissionSetAssignment WHERE "
                                    f"Assignee.Username = {literal(staff_username)}")
    checks.append(Check("staff user has NABD_Staff", assigned(staff_sets, s.STAFF_PERMISSION_SET)))
    username = integration_username(org_id)
    user = await _one(client, "SELECT Id, IsActive, Profile.Name, Profile.UserLicense.Name FROM User "
                              f"WHERE Username = {literal(username)}")
    checks.append(Check("integration user active with API-only integration profile",
                        bool(user and user["IsActive"] and user["Profile"]["Name"] == s.INTEGRATION_PROFILE)))
    if user:
        sets = await client.query("SELECT PermissionSet.Name FROM PermissionSetAssignment WHERE "
                                  f"AssigneeId = {literal(user['Id'])} AND PermissionSet.IsOwnedByProfile = false")
        names = sorted(row["PermissionSet"]["Name"] for row in sets)
        checks.append(_compare("integration user permission sets", [s.INTEGRATION_PERMISSION_SET], names))
        groups = await client.query("SELECT Group.DeveloperName FROM GroupMember WHERE "
                                    f"UserOrGroupId = {literal(user['Id'])}")
        checks.append(_compare("integration user groups", [s.INTEGRATION_GROUP],
                               sorted(row["Group"]["DeveloperName"] for row in groups)))
    approvals = await client.query(
        "SELECT Parent.Name FROM SetupEntityAccess WHERE SetupEntityId IN (SELECT Id FROM "
        f"ExternalClientApplication WHERE DeveloperName = {literal(s.EXTERNAL_CLIENT_APP)})")
    checks.append(_compare("client app approved permission sets", [s.INTEGRATION_PERMISSION_SET],
                           sorted(row["Parent"]["Name"] for row in approvals)))
    return checks
