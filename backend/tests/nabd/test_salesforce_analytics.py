"""Phase 6: the analytics contract, its generated metadata and reconciliation, and the risk
engine's fixed names. No Salesforce calls; the real org is checked by `analytics-reconcile`,
`verify` and `risk-smoke`."""
import asyncio
import json
import re
import xml.etree.ElementTree as ET

import pytest

from nabd.salesforce import analytics as a
from nabd.salesforce import fixtures as fx
from nabd.salesforce import metadata
from nabd.salesforce import schema as s

NS = {"m": metadata.NS}
# Standard report columns used by the contract, as the org's report types describe them.
STANDARD_COLUMNS = {
    "CaseList": {"CASE_NUMBER", "SUBJECT", "STATUS", "PRIORITY", "ACCOUNT.NAME", "LAST_UPDATE", "CREATED_DATEONLY"},
    "CustomEntity$Risk_Episode__c": {"CUST_NAME", "CUST_CREATED_DATE"},
    "CustomEntity$Risk_Rule__c": {"CUST_NAME", "CUST_CREATED_DATE"},
}
REPORT_OBJECT = {"CaseList": "Case", "CustomEntity$Risk_Episode__c": "Risk_Episode__c",
                 "CustomEntity$Risk_Rule__c": "Risk_Rule__c"}


def _column_exists(report: a.Report, column: str) -> bool:
    if column in STANDARD_COLUMNS[report.report_type]:
        return True
    owner, _, field = column.partition(".")
    if owner != REPORT_OBJECT[report.report_type]:
        return False
    s.sobject(owner).field(field)  # raises if the field is not in the schema contract
    return True


def test_reports_use_contract_fields_and_salesforce_limits():
    assert len({report.name for report in a.REPORTS}) == len(a.REPORTS)
    for report in a.REPORTS:
        assert len(report.name) <= 40 and len(report.label) <= 40 and len(report.description) <= 255, report.name
        assert report.sobject == REPORT_OBJECT[report.report_type]
        used = [*report.columns, *(item.column for item in report.filters)]
        if report.grouping:
            used.append(report.grouping.column)
        if report.sort_column:
            used.append(report.sort_column)
        for column in used:
            assert _column_exists(report, column), f"{report.name}: {column}"
        if report.tabular:
            # A list report on a dashboard needs a row limit, a sort, and a SOQL key for reconciliation.
            assert report.row_limit and report.sort_column and report.sort_soql and report.key_soql, report.name
        if report.report_type == "CaseList":
            assert a.NABD_CASE in report.filters, f"{report.name} must count NABD complaints only"


def test_groupings_that_soql_cannot_group_are_formula_fields():
    for report in a.REPORTS:
        if report.grouping and not report.grouping.groupable:
            field = s.sobject(report.sobject).field(report.grouping.soql)
            assert field.formula, report.name


def test_dashboards_fit_the_grid_and_use_the_right_kind_of_report():
    for dashboard in a.DASHBOARDS:
        assert len(dashboard.description) <= 255 and len(dashboard.title) <= 80, dashboard.name
        cells: set[tuple[int, int]] = set()
        for component in dashboard.components:
            report = a.REPORTS_BY_NAME[component.report]
            assert (component.kind == "FlexTable") == report.tabular, component.header
            assert 0 <= component.column and component.column + component.columns <= 12, component.header
            area = {(row, column) for row in range(component.row, component.row + component.rows)
                    for column in range(component.column, component.column + component.columns)}
            assert not cells & area, f"{dashboard.name}: {component.header} overlaps"
            cells |= area
    shown = {component.report for dashboard in a.DASHBOARDS for component in dashboard.components}
    assert shown == set(a.REPORTS_BY_NAME), "every report appears on a dashboard"


def test_the_dashboards_cover_the_phase_6_scope():
    headers = " | ".join(c.header.lower() for d in a.DASHBOARDS for c in d.components)
    for topic in ("per day", "category", "language", "sentiment", "priority", "reopened", "age", "outcome",
                  "freshness", "warnings"):
        assert topic in headers, topic


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    if not metadata.BASE_LAYOUTS.exists():
        pytest.skip("salesforce/ is not available in this test environment")
    return metadata.render()


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text.encode())


def test_report_metadata_matches_the_contract(rendered):
    for report in a.REPORTS:
        root = _xml(rendered[f"reports/{a.REPORT_FOLDER}/{report.name}.report-meta.xml"])
        columns = [item.text for item in root.findall("m:columns/m:field", NS)]
        grouping = root.find("m:groupingsDown/m:field", NS)
        assert root.find("m:format", NS).text == ("Tabular" if report.tabular else "Summary")
        if report.grouping:
            assert grouping.text == report.grouping.column
            assert report.grouping.column not in columns  # Salesforce refuses a grouping as a column
        else:
            assert grouping is None and root.find("m:rowLimit", NS).text == str(report.row_limit)
        criteria = [(item.find("m:column", NS).text, item.find("m:operator", NS).text, item.find("m:value", NS).text or "")
                    for item in root.findall("m:filter/m:criteriaItems", NS)]
        assert criteria == [(item.column, item.operator, item.value) for item in report.filters]
        interval = root.find("m:timeFrameFilter/m:interval", NS).text
        assert interval == (f"INTERVAL_LAST{report.created_last_days}" if report.created_last_days
                            else "INTERVAL_CUSTOM")


def test_dashboards_are_dynamic_and_folders_are_shared_only_with_staff(rendered):
    for dashboard in a.DASHBOARDS:
        text = rendered[f"dashboards/{a.DASHBOARD_FOLDER}/{dashboard.name}.dashboard-meta.xml"]
        root = _xml(text)
        assert root.find("m:dashboardType", NS).text == "LoggedInUser"
        assert root.find("m:runningUser", NS) is None  # no org-specific username in Git
        for table in root.iter(f"{{{metadata.NS}}}flexComponentProperties"):
            assert table.find("m:flexTableSortInfo/m:sortColumn", NS) is not None
        reports = {item.text for item in root.iter(f"{{{metadata.NS}}}report")}
        assert reports == {f"{a.REPORT_FOLDER}/{component.report}" for component in dashboard.components}
    for path in (f"reports/{a.REPORT_FOLDER}.reportFolder-meta.xml",
                 f"dashboards/{a.DASHBOARD_FOLDER}.dashboardFolder-meta.xml"):
        shares = _xml(rendered[path]).findall("m:folderShares", NS)
        assert [(item.find("m:sharedTo", NS).text, item.find("m:accessLevel", NS).text) for item in shares] == [
            (s.STAFF_GROUP, "View")]


def test_integration_permission_set_reaches_no_risk_or_sentiment_data(rendered):
    text = rendered[f"permissionsets/{s.INTEGRATION_PERMISSION_SET}.permissionset-meta.xml"]
    for name in ("Risk_Rule__c", "Risk_Episode__c", "Risk_Evidence__c", "Complaint_Evidence__c",
                 "Negative_Streak__c", "Sentiment__c", "NABD_Open_Age_Band__c", "Task."):
        assert name not in text, name


# --- SOQL and reconciliation ------------------------------------------------------------


def test_every_report_has_an_equivalent_soql_condition():
    by_category = a.REPORTS_BY_NAME["NABD_Complaints_by_Category"]
    assert by_category.where() == "NABD_Issue_Id__c != null"
    recent = a.REPORTS_BY_NAME["NABD_Complaints_Last_30_Days"]
    assert recent.where() == "NABD_Issue_Id__c != null AND CreatedDate = LAST_N_DAYS:30"
    aged = a.REPORTS_BY_NAME["NABD_Open_Complaints_by_Age"]
    assert aged.where() == "NABD_Issue_Id__c != null AND Status != 'Closed'"
    assert a.REPORTS_BY_NAME["NABD_Active_Warnings_by_Rule"].where() == "Active_Key__c != null"
    assert a.REPORTS_BY_NAME["NABD_Risk_Rule_Freshness"].where() == ""


def _summary_payload(groups: dict[str | None, int]) -> dict:
    groupings = [{"key": str(index), "value": value, "label": value or "-"} for index, value in enumerate(groups)]
    facts = {f"{index}!T": {"aggregates": [{"value": count}]} for index, count in enumerate(groups.values())}
    facts["T!T"] = {"aggregates": [{"value": sum(groups.values())}]}
    return {"groupingsDown": {"groupings": groupings}, "factMap": facts}


def test_report_results_are_parsed_into_totals_and_groups():
    total, groups = a.report_counts(_summary_payload({"Delivery": 3, None: 1}))
    assert total == 4 and groups == {"Delivery": 3, "": 1}
    rows = a.report_rows({"factMap": {"T!T": {"rows": [{"dataCells": [{"label": "00001029"}]},
                                                       {"dataCells": [{"label": "00001030"}]}]}}})
    assert rows == ["00001029", "00001030"]


class FakeOrg:
    """Answers the reconciliation's queries: report IDs, report runs and SOQL."""

    def __init__(self, *, drift: bool = False):
        self.drift = drift
        self.queries: list[str] = []
        self.base_path = "/services/data/v68.0"

    async def query(self, soql: str) -> list[dict]:
        self.queries.append(soql)
        if soql.startswith("SELECT Id, DeveloperName FROM Report"):
            return [{"Id": f"00O{index:015d}", "DeveloperName": report.name} for index, report in enumerate(a.REPORTS)]
        if " grouped, COUNT(Id) total " in soql:
            return [{"grouped": "Delivery", "total": 3}, {"grouped": None, "total": 1}]
        if soql.startswith("SELECT NABD_Open_Age_Band__c FROM Case"):
            return [{"NABD_Open_Age_Band__c": "1. Under 1 day"}] * 3 + [{"NABD_Open_Age_Band__c": None}]
        key = re.match(r"SELECT (\w+) FROM", soql).group(1)
        return [{key: "A"}, {key: "B"}]

    async def request(self, method: str, path: str, *, params=None, **_):
        index = int(path.rsplit("/", 1)[1][3:])
        report = a.REPORTS[index]
        if report.tabular:
            return {"factMap": {"T!T": {"rows": [{"dataCells": [{"label": "B"}]}, {"dataCells": [{"label": "A"}]}]}}}
        if report.grouping and not report.grouping.groupable:
            return _summary_payload({"1. Under 1 day": 3, None: 1})
        return _summary_payload({"Delivery": 3 if not self.drift else 2, None: 1})


def test_reconciliation_passes_when_reports_and_soql_agree():
    org = FakeOrg()
    checks = asyncio.run(a.reconcile(org))
    assert all(check.passed for check in checks), [c for c in checks if not c.passed]
    assert len(checks) == len(a.REPORTS) + 2
    assert any("GROUP BY Complaint_Category__c" in soql for soql in org.queries)


def test_reconciliation_reports_any_difference():
    checks = asyncio.run(a.reconcile(FakeOrg(drift=True)))
    failed = [check for check in checks if not check.passed]
    assert failed and all("!= SOQL" in check.detail or "agree" in check.name for check in failed)


# --- Risk engine names shared by Python and Apex -------------------------------------------


def test_fixture_rules_are_the_engine_s_rules_with_valid_parameters():
    rules = {rule.code: json.loads(rule.parameters) for rule in fx.RISK_RULES}
    assert set(rules) == set(s.RISK_RULE_CODES)
    assert rules["repeat_complaints"] == {"distinct_complaints": 3, "window_days": 30}
    assert rules["high_priority_unresolved"] == {"priority": "High", "unresolved_hours": 24}
    assert rules["persistent_negative_sentiment"] == {"follow_ups": 1}


def test_apex_names_match_the_python_contract():
    root = metadata.SOURCE_ROOT
    if not root.exists():
        pytest.skip("salesforce/ is not available in this test environment")
    for name in s.RISK_CLASSES:
        assert (root / "classes" / f"{name}.cls").exists(), name
    for name in s.RISK_TRIGGERS:
        assert (root / "triggers" / f"{name}.trigger").exists(), name
    schedule = (root / "classes" / "NabdRiskSchedule.cls").read_text()
    assert f"JOB_NAME = '{s.RISK_SCHEDULE_JOB}'" in schedule
    assert f"CRON = '{s.RISK_SCHEDULE_CRON}'" in schedule
    engine = (root / "classes" / "NabdRiskEngine.cls").read_text()
    for code in s.RISK_RULE_CODES:
        assert f"'{code}'" in engine, code
    gateway = (root / "classes" / "NabdCaseCommands.cls").read_text()
    assert "'customer_report', 'customer_confirmation'" in gateway  # schema.CONTACT_EVIDENCE_KINDS
    assert tuple(s.CONTACT_EVIDENCE_KINDS) == ("customer_report", "customer_confirmation")


def test_deploy_pauses_the_hourly_job_and_always_schedules_it_again(monkeypatch):
    from nabd.salesforce import cli

    calls = []

    async def schedule(target_org, action):
        calls.append(action)
        return True  # the job existed

    monkeypatch.setattr(cli, "_schedule", schedule)
    monkeypatch.setattr(cli.metadata, "check", lambda: [])
    monkeypatch.setattr(cli, "_sf", lambda *arguments, env=None: calls.append("deploy") or 1)  # deploy fails
    assert cli._deploy("nabd-dev") == 1
    assert calls == ["pause", "deploy", "resume"]

    calls.clear()

    async def missing(target_org, action):
        calls.append(action)
        return False  # nothing scheduled yet (first deployment)

    monkeypatch.setattr(cli, "_schedule", missing)
    monkeypatch.setattr(cli, "_sf", lambda *arguments, env=None: calls.append("deploy") or 0)
    assert cli._deploy("nabd-dev") == 0
    assert calls == ["pause", "deploy"]
