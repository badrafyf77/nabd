"""The NABD analytics contract (Phase 6): Salesforce reports, dashboards and their reconciliation.

One ``Report`` definition gives both the deployed report metadata (rendered by
``metadata``) and the SOQL that must return the same numbers. ``reconcile``
runs every report through the Analytics REST API as the developer and compares
its grand total and every group (or, for list reports, its rows) with SOQL.

A complaint is a Case with ``NABD_Issue_Id__c``: one distinct issue. Counts are
cases, never messages. Report filters are kept to simple comparisons so each
has an exact SOQL equivalent.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from nabd.salesforce.rest import SalesforceClient

REPORT_FOLDER = "NABD_Analytics"
REPORT_FOLDER_LABEL = "NABD Analytics"
DASHBOARD_FOLDER = "NABD_Dashboards"
DASHBOARD_FOLDER_LABEL = "NABD Dashboards"
SHARED_WITH_GROUP = "NABD_Staff"  # read-only access for staff; the integration user gets none


@dataclass(frozen=True)
class Filter:
    column: str  # report column
    operator: Literal["equals", "notEqual", "greaterThan"]
    value: str
    soql: str  # the same condition in SOQL


@dataclass(frozen=True)
class Grouping:
    column: str  # report column
    soql: str  # SOQL field or expression
    groupable: bool = True  # formula fields cannot be grouped in SOQL: count the rows instead


@dataclass(frozen=True)
class Report:
    name: str
    label: str
    description: str
    report_type: str
    sobject: str
    columns: tuple[str, ...]
    filters: tuple[Filter, ...] = ()
    grouping: Grouping | None = None  # summary report; None is a tabular (list) report
    created_last_days: int | None = None
    sort_column: str | None = None
    sort_soql: str | None = None
    sort_descending: bool = False
    row_limit: int | None = None
    key_soql: str | None = None  # list reports: the SOQL field behind columns[0]

    @property
    def tabular(self) -> bool:
        return self.grouping is None

    @property
    def date_column(self) -> str:
        return "CREATED_DATEONLY" if self.report_type == "CaseList" else "CUST_CREATED_DATE"

    def where(self) -> str:
        conditions = [item.soql for item in self.filters]
        if self.created_last_days:
            conditions.append(f"CreatedDate = LAST_N_DAYS:{self.created_last_days}")
        return " AND ".join(conditions)

    def where_clause(self) -> str:
        return f" WHERE {self.where()}" if self.where() else ""


NABD_CASE = Filter("Case.NABD_Issue_Id__c", "notEqual", "", "NABD_Issue_Id__c != null")
OPEN = Filter("STATUS", "notEqual", "Closed", "Status != 'Closed'")
CLOSED = Filter("STATUS", "equals", "Closed", "Status = 'Closed'")
REOPENED = Filter("Case.Reopen_Count__c", "greaterThan", "0", "Reopen_Count__c > 0")
ACTIVE_WARNING = Filter("Risk_Episode__c.Active_Key__c", "notEqual", "", "Active_Key__c != null")

CASE_COLUMNS = ("CASE_NUMBER", "SUBJECT", "STATUS", "ACCOUNT.NAME")
EPISODE = "CustomEntity$Risk_Episode__c"
EPISODE_COLUMNS = ("CUST_NAME", "Risk_Episode__c.Account__c", "Risk_Episode__c.Rule_Code__c",
                   "Risk_Episode__c.Opened_At__c")


def _complaints_by(name: str, label: str, column: str, soql: str, description: str, *,
                   filters: tuple[Filter, ...] = (), groupable: bool = True, days: int | None = None) -> Report:
    return Report(name, label, description, "CaseList", "Case", CASE_COLUMNS, (NABD_CASE, *filters),
                  Grouping(column, soql, groupable), created_last_days=days)


REPORTS: tuple[Report, ...] = (
    _complaints_by("NABD_Complaints_by_Category", "Complaints by category", "Case.Complaint_Category__c",
                   "Complaint_Category__c", "Every NABD complaint (distinct issue) by department category."),
    _complaints_by("NABD_Complaints_by_Language", "Complaints by language", "Case.Customer_Language__c",
                   "Customer_Language__c", "Every NABD complaint by the customer's language."),
    _complaints_by("NABD_Complaints_by_Sentiment", "Complaints by sentiment", "Case.Sentiment__c", "Sentiment__c",
                   "Every NABD complaint by NABD's latest internal sentiment assessment. Staff only."),
    _complaints_by("NABD_Complaints_by_Priority", "Complaints by priority", "PRIORITY", "Priority",
                   "Every NABD complaint by its current priority (staff changes included)."),
    _complaints_by("NABD_Complaints_Last_30_Days", "Complaints per day, last 30 days", "CREATED_DATEONLY",
                   "DAY_ONLY(convertTimezone(CreatedDate))",
                   "NABD complaints opened per day in the last 30 days (viewer's time zone).", days=30),
    _complaints_by("NABD_Open_Complaints_by_Age", "Open complaints by age", "Case.NABD_Open_Age_Band__c",
                   "NABD_Open_Age_Band__c", "Open NABD complaints by time since they were opened or reopened.",
                   filters=(OPEN,), groupable=False),
    _complaints_by("NABD_Resolution_Outcomes", "Closed complaints by outcome", "Case.Resolution_Outcome__c",
                   "Resolution_Outcome__c", "Closed NABD complaints by resolution outcome.", filters=(CLOSED,)),
    _complaints_by("NABD_Reopened_by_Category", "Reopened complaints by category", "Case.Complaint_Category__c",
                   "Complaint_Category__c", "Recurring problems: NABD complaints reopened at least once.",
                   filters=(REOPENED,)),
    Report("NABD_Latest_Complaint_Activity", "Latest complaint activity",
           "The ten most recently changed NABD complaints: how current the complaint data is.",
           "CaseList", "Case", ("CASE_NUMBER", "SUBJECT", "STATUS", "LAST_UPDATE"), (NABD_CASE,),
           sort_column="LAST_UPDATE", sort_soql="LastModifiedDate", sort_descending=True, row_limit=10,
           key_soql="CaseNumber"),
    Report("NABD_Active_Warnings_by_Rule", "Active warnings by rule",
           "Active NABD risk warnings (one per customer and rule) by rule.", EPISODE, "Risk_Episode__c",
           EPISODE_COLUMNS, (ACTIVE_WARNING,), Grouping("Risk_Episode__c.Rule_Code__c", "Rule_Code__c")),
    Report("NABD_Warnings_by_Status", "Warnings by status",
           "Every NABD risk warning, active and cleared: the warning history.", EPISODE, "Risk_Episode__c",
           EPISODE_COLUMNS, (), Grouping("Risk_Episode__c.Status__c", "Status__c")),
    Report("NABD_Active_Warning_List", "Active warnings",
           "Active NABD risk warnings, newest first, with when the engine last evaluated them.", EPISODE,
           "Risk_Episode__c",
           ("CUST_NAME", "Risk_Episode__c.Account__c", "Risk_Episode__c.Rule_Code__c",
            "Risk_Episode__c.Opened_At__c", "Risk_Episode__c.Evidence_Count__c",
            "Risk_Episode__c.Last_Evaluated_At__c"),
           (ACTIVE_WARNING,), sort_column="Risk_Episode__c.Opened_At__c", sort_soql="Opened_At__c",
           sort_descending=True, row_limit=25, key_soql="Name"),
    Report("NABD_Risk_Rule_Freshness", "Risk rules and last full evaluation",
           "Every risk rule version, whether it is active, when the hourly sweep last evaluated it and any "
           "configuration error: how fresh the warnings are.", "CustomEntity$Risk_Rule__c", "Risk_Rule__c",
           ("Risk_Rule__c.Rule_Key__c", "CUST_NAME", "Risk_Rule__c.Active__c", "Risk_Rule__c.Last_Evaluated_At__c",
            "Risk_Rule__c.Last_Error__c"),
           sort_column="Risk_Rule__c.Rule_Key__c", sort_soql="Rule_Key__c", row_limit=25, key_soql="Rule_Key__c"),
)
REPORTS_BY_NAME = {report.name: report for report in REPORTS}
# Reports over every NABD complaint: their grand totals must all be equal.
ALL_COMPLAINTS = ("NABD_Complaints_by_Category", "NABD_Complaints_by_Language", "NABD_Complaints_by_Sentiment",
                  "NABD_Complaints_by_Priority")


@dataclass(frozen=True)
class Component:
    kind: Literal["Metric", "Bar", "Column", "Donut", "FlexTable"]
    report: str
    header: str
    row: int
    column: int
    rows: int
    columns: int
    footer: str | None = None


@dataclass(frozen=True)
class Dashboard:
    name: str
    title: str
    description: str
    components: tuple[Component, ...]


FRESHNESS = "The time at the top is when this dashboard was last refreshed; press Refresh for current data."

DASHBOARDS: tuple[Dashboard, ...] = (
    Dashboard(
        "NABD_Complaints", "NABD Complaints",
        "NABD complaints: volume, category, language, sentiment, priority, recurring problems, age and "
        "outcomes. " + FRESHNESS,
        (
            Component("Metric", "NABD_Complaints_Last_30_Days", "Complaints, last 30 days", 0, 0, 4, 3),
            Component("Metric", "NABD_Open_Complaints_by_Age", "Open complaints", 0, 3, 4, 3),
            Component("Metric", "NABD_Reopened_by_Category", "Reopened complaints", 0, 6, 4, 3,
                      footer="Recurring problems"),
            Component("Metric", "NABD_Active_Warnings_by_Rule", "Active risk warnings", 0, 9, 4, 3),
            Component("Column", "NABD_Complaints_Last_30_Days", "Complaints per day", 4, 0, 8, 6),
            Component("Donut", "NABD_Complaints_by_Category", "By category", 4, 6, 8, 6),
            Component("Bar", "NABD_Complaints_by_Language", "By language", 12, 0, 8, 4),
            Component("Bar", "NABD_Complaints_by_Sentiment", "By sentiment (internal)", 12, 4, 8, 4),
            Component("Bar", "NABD_Complaints_by_Priority", "By priority", 12, 8, 8, 4),
            Component("Column", "NABD_Open_Complaints_by_Age", "Open complaints by age", 20, 0, 8, 4,
                      footer="Since opened or reopened"),
            Component("Donut", "NABD_Resolution_Outcomes", "Closed complaints by outcome", 20, 4, 8, 4),
            Component("Bar", "NABD_Reopened_by_Category", "Reopened complaints by category", 20, 8, 8, 4),
            Component("FlexTable", "NABD_Latest_Complaint_Activity", "Latest complaint activity (data freshness)",
                      28, 0, 8, 12),
        ),
    ),
    Dashboard(
        "NABD_Risk_Warnings", "NABD Risk Warnings",
        "Early warnings from versioned risk rules, one per customer and rule with one task. Rules run after "
        "case changes and hourly. " + FRESHNESS,
        (
            Component("Metric", "NABD_Active_Warnings_by_Rule", "Active warnings", 0, 0, 6, 4),
            Component("Bar", "NABD_Active_Warnings_by_Rule", "Active warnings by rule", 0, 4, 6, 4),
            Component("Donut", "NABD_Warnings_by_Status", "All warnings by status", 0, 8, 6, 4),
            Component("FlexTable", "NABD_Active_Warning_List", "Active warnings", 6, 0, 8, 12),
            Component("FlexTable", "NABD_Risk_Rule_Freshness", "Rules and last full evaluation (freshness)",
                      14, 0, 7, 12),
        ),
    ),
)


# --- Reconciliation ----------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def _key(value: Any) -> str:
    """One form for group values from reports and SOQL: empty values become ''."""
    if value is None or value == "" or value == "-":
        return ""
    return str(value)


def report_counts(payload: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """(grand total, {group value: count}) from an Analytics API summary report result."""
    facts = payload["factMap"]
    total = int(facts["T!T"]["aggregates"][0]["value"])
    groups: dict[str, int] = {}
    for group in payload.get("groupingsDown", {}).get("groupings", []):
        count = int(facts[f"{group['key']}!T"]["aggregates"][0]["value"])
        groups[_key(group.get("value"))] = groups.get(_key(group.get("value")), 0) + count
    return total, groups


def report_rows(payload: dict[str, Any]) -> list[str]:
    """First-column values of a tabular report result, in report order."""
    rows = payload["factMap"]["T!T"].get("rows", [])
    return [_key(row["dataCells"][0].get("label")) for row in rows]


async def soql_counts(client: SalesforceClient, report: Report) -> tuple[int, dict[str, int]]:
    assert report.grouping is not None
    where = report.where_clause()
    if report.grouping.groupable:
        rows = await client.query(f"SELECT {report.grouping.soql} grouped, COUNT(Id) total FROM {report.sobject}"
                                  f"{where} GROUP BY {report.grouping.soql}")
        groups = {_key(row["grouped"]): int(row["total"]) for row in rows}
    else:
        rows = await client.query(f"SELECT {report.grouping.soql} FROM {report.sobject}{where}")
        groups = dict(Counter(_key(row[report.grouping.soql]) for row in rows))
    return sum(groups.values()), groups


async def soql_rows(client: SalesforceClient, report: Report) -> list[str]:
    order = f" ORDER BY {report.sort_soql}{' DESC' if report.sort_descending else ''}" if report.sort_soql else ""
    rows = await client.query(f"SELECT {report.key_soql} FROM {report.sobject}{report.where_clause()}{order} "
                              f"LIMIT {report.row_limit}")
    return [_key(row[report.key_soql]) for row in rows]


async def reconcile(client: SalesforceClient) -> list[Check]:
    """Run every NABD report and compare it with SOQL over the same records."""
    found = await client.query("SELECT Id, DeveloperName FROM Report WHERE FolderName = "
                               f"'{REPORT_FOLDER_LABEL}'")
    ids = {row["DeveloperName"]: row["Id"] for row in found}
    checks = [Check("reports deployed", set(ids) == set(REPORTS_BY_NAME),
                    ", ".join(sorted(set(REPORTS_BY_NAME) ^ set(ids))))]
    totals: dict[str, int] = {}
    for report in REPORTS:
        if report.name not in ids:
            continue
        try:
            passed, detail = await _reconcile_one(client, report, ids[report.name], totals)
        except Exception as error:  # report every check; one failure must not hide the others
            passed, detail = False, f"{type(error).__name__}: {error}"
        checks.append(Check(f"report {report.name} matches SOQL", passed, detail))
    complaint_totals = {name: totals[name] for name in ALL_COMPLAINTS if name in totals}
    checks.append(Check("complaint reports agree on one total",
                        len(complaint_totals) == len(ALL_COMPLAINTS) and len(set(complaint_totals.values())) == 1,
                        ", ".join(f"{name.removeprefix('NABD_Complaints_by_')} {value}"
                                  for name, value in complaint_totals.items())))
    return checks


async def _reconcile_one(client: SalesforceClient, report: Report, report_id: str,
                         totals: dict[str, int]) -> tuple[bool, str]:
    payload = await client.request("GET", f"{client.base_path}/analytics/reports/{report_id}",
                                   params={"includeDetails": "true" if report.tabular else "false"})
    if report.tabular:
        actual = report_rows(payload)
        expected = await soql_rows(client, report)
        # Ties in the sort column may order rows differently; the same rows must appear.
        passed = sorted(actual) == sorted(expected)
        return passed, f"{len(actual)} rows" if passed else f"report {actual} != SOQL {expected}"
    total, groups = report_counts(payload)
    expected_total, expected_groups = await soql_counts(client, report)
    totals[report.name] = total
    passed = total == expected_total and groups == expected_groups
    return passed, (f"total {total}, {len(groups)} groups" if passed else
                    f"report {total} {groups} != SOQL {expected_total} {expected_groups}")
