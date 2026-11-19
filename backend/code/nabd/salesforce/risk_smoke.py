"""``python -m nabd salesforce risk-smoke``: the warning lifecycle in the real org (Phase 6).

NABD's own writes go through the real case gateway as the integration user
(client credentials from ``backend/.env.nabd``); staff actions and reads of
warnings use the developer's ``sf`` CLI session, because the integration user
cannot read warnings, tasks or sentiment (checked first). Smoke customer C008:

1. no active warning for C008 before the run;
2. labelled smoke complaints are created until ``repeat_complaints`` holds:
   one warning, one open task, one evidence row per counted complaint;
3. a negative follow-up on the last one makes ``persistent_negative_sentiment``
   hold; replaying the same command changes nothing;
4. another follow-up re-evaluates both: still one warning and one task each;
5. staff close the smoke complaints as duplicates: both warnings clear, their
   tasks are completed with a note, their evidence stays as history.

Each run opens new warnings (a recurrence) and leaves its complaints closed as
duplicates, which no rule counts, so the next run starts from the same state.
Evaluation is asynchronous in Salesforce, so each step waits for its result.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from nabd.salesforce import schema as s
from nabd.salesforce.commands import CaseCommand, CommandResult, Evidence, SalesforceCommands, salesforce_time
from nabd.salesforce.rest import ClientCredentialsAuth, CliOrgAuth, SalesforceClient, SalesforceRequestError
from nabd.salesforce.soql import literal
from nabd.settings import SalesforceSettings

CUSTOMER = "C008"
REPEAT, NEGATIVE = "repeat_complaints", "persistent_negative_sentiment"
WAIT_SECONDS = 90


@dataclass
class Result:
    name: str
    passed: bool
    detail: str = ""


class RiskSmoke:
    def __init__(self, admin: SalesforceClient, integration: SalesforceClient):
        self.admin = admin
        self.integration = integration
        self.gateway = SalesforceCommands(integration)
        self.results: list[Result] = []
        self.run_id = uuid.uuid4().hex[:12]
        self.cases: list[dict[str, Any]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append(Result(name, passed, detail))
        return passed

    # --- Reads as the developer -----------------------------------------------------------

    async def episodes(self, code: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        condition = " AND Active_Key__c != null" if active_only else ""
        return await self.admin.query(
            "SELECT Id, Name, Episode_Key__c, Active_Key__c, Status__c, Explanation__c, Evidence_Count__c, "
            "Follow_Up_Task_Id__c, Clear_Reason__c, Rule_Version__c, Risk_Rule__r.OwnerId, "
            "(SELECT Id, Case__c, Complaint_Evidence__c, Current__c FROM Risk_Evidence__r) "
            f"FROM {s.RISK_EPISODE.name} WHERE Account__r.ShopEasy_Customer_Id__c = {literal(CUSTOMER)} "
            f"AND Rule_Code__c = {literal(code)}{condition} ORDER BY Opened_At__c")

    async def task(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        rows = await self.admin.query("SELECT Id, IsClosed, Status, OwnerId, WhatId, Description, "
                                      f"NABD_Follow_Up_Type__c FROM Task WHERE Id = {literal(task_id)}")
        return rows[0] if rows else None

    async def tasks_for(self, episode_id: str) -> list[dict[str, Any]]:
        return await self.admin.query(f"SELECT Id, IsClosed FROM Task WHERE WhatId = {literal(episode_id)}")

    async def wait(self, condition: Callable[[], Awaitable[bool]]) -> bool:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if await condition():
                return True
            await asyncio.sleep(3)
        return False

    async def rule(self, code: str) -> dict[str, Any]:
        rows = await self.admin.query(f"SELECT Id, OwnerId, Parameters_JSON__c, Version__c FROM {s.RISK_RULE.name} "
                                      f"WHERE Rule_Code__c = {literal(code)} AND Active__c = true "
                                      "ORDER BY Version__c DESC LIMIT 1")
        if not rows:
            raise RuntimeError(f"No active {code} rule; run seed first.")
        return rows[0]

    # --- Writes ------------------------------------------------------------------------------

    def _evidence(self, ref: str, text: str) -> Evidence:
        return Evidence(ref=ref, kind="customer_report", text=text, language="en", script="latin",
                        captured_at=salesforce_time(datetime.now(timezone.utc)))

    async def create(self, index: int, sentiment: str) -> CommandResult:
        command = CaseCommand(
            operation_id=f"risk-smoke-{self.run_id}-create-{index}", operation="create_case", customer_id=CUSTOMER,
            conversation_id=f"risk-smoke-{self.run_id}", issue_id=f"risk-smoke-{self.run_id}-issue-{index}",
            category="Orders_Products", subject=f"NABD risk smoke {self.run_id} #{index} (test, closed afterwards)",
            summary="Risk smoke test complaint created by python -m nabd salesforce risk-smoke.",
            language="en", sentiment=sentiment, urgency="low", confidence=0.9, needs_review=False, priority="Low",
            priority_reason="Low: no stronger signal",
            evidence=(self._evidence(f"risk-smoke-{self.run_id}-msg-{index}",
                                     "Risk smoke test: my order has a problem."),),
        )
        return await self.gateway.execute(command)

    async def follow_up(self, case: dict[str, Any], step: str, sentiment: str) -> tuple[CaseCommand, CommandResult]:
        rows = await self.admin.query(f"SELECT SystemModstamp FROM Case WHERE Id = {literal(case['case_id'])}")
        stamp = datetime.strptime(rows[0]["SystemModstamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
        command = CaseCommand(
            operation_id=f"risk-smoke-{self.run_id}-{step}", operation="update_case", customer_id=CUSTOMER,
            conversation_id=f"risk-smoke-{self.run_id}", case_id=case["case_id"],
            expected_version=salesforce_time(stamp), sentiment=sentiment,
            evidence=(self._evidence(f"risk-smoke-{self.run_id}-{step}",
                                     "Risk smoke test follow-up: still not sorted."),),
        )
        return command, await self.gateway.execute(command)

    # --- Steps ---------------------------------------------------------------------------

    async def run(self) -> list[Result]:
        await self.privacy()
        repeat_rule = await self.rule(REPEAT)
        threshold = int(json.loads(repeat_rule["Parameters_JSON__c"])["distinct_complaints"])
        before_repeat = await self.episodes(REPEAT)
        before_negative = await self.episodes(NEGATIVE)
        if not self.check("no active C008 warning before the run", not before_repeat and not before_negative,
                          f"{len(before_repeat)} repeat, {len(before_negative)} sentiment"):
            return self.results
        history = {code: len(await self.episodes(code, active_only=False)) for code in (REPEAT, NEGATIVE)}
        counted = await self.counted_complaints()
        needed = threshold - counted
        if not self.check("C008 is below the repeat threshold", needed >= 1,
                          f"{counted} counted complaints, threshold {threshold}"):
            return self.results
        for index in range(1, needed + 1):
            result = await self.create(index, "negative" if index == needed else "neutral")
            self.cases.append({"case_id": result.case_id, "case_number": result.case_number})
            self.check(f"smoke complaint {index} created", result.status == "confirmed",
                       f"{result.case_number} {result.status}")
        try:
            await self.repeat_warning(threshold, repeat_rule)
            await self.negative_warning()
            await self.stays_single()
        finally:
            await self.close_as_duplicates()
        await self.cleared(history)
        return self.results

    async def privacy(self) -> None:
        for name, soql in (("integration user cannot read risk warnings", f"SELECT Id FROM {s.RISK_EPISODE.name} LIMIT 1"),
                           ("integration user cannot read the negative streak",
                            "SELECT Negative_Streak__c FROM Case LIMIT 1")):
            try:
                await self.integration.query(soql)
                self.check(name, False, "the query was answered")
            except SalesforceRequestError as error:
                self.check(name, True, ", ".join(error.codes) or "refused")

    async def counted_complaints(self) -> int:
        rows = await self.admin.query(
            "SELECT Id FROM Case WHERE Account.ShopEasy_Customer_Id__c = " + literal(CUSTOMER)
            + " AND NABD_Issue_Id__c != null AND (Resolution_Outcome__c = null OR Resolution_Outcome__c != 'duplicate')"
            " AND (CreatedDate = LAST_N_DAYS:30 OR Last_Reopened_At__c = LAST_N_DAYS:30)")
        return len(rows)

    async def repeat_warning(self, threshold: int, rule: dict[str, Any]) -> None:
        async def opened() -> bool:
            return len(await self.episodes(REPEAT)) == 1
        found = await self.wait(opened)
        episodes = await self.episodes(REPEAT)
        if not self.check("repeat-complaints warning opened once", found and len(episodes) == 1,
                          f"{len(episodes)} active"):
            return
        episode = episodes[0]
        current = [row for row in episode.get("Risk_Evidence__r") or [] if row["Current__c"]]
        explanation = episode["Explanation__c"] or ""
        self.check("warning explains every counted complaint",
                   len(current) >= threshold and all(case["case_number"] in explanation for case in self.cases),
                   f"{episode['Name']}: {len(current)} evidence rows; " + explanation.splitlines()[1][:90])
        task = await self.task(episode["Follow_Up_Task_Id__c"])
        self.check("one open follow-up task for the rule owner",
                   bool(task) and not task["IsClosed"] and task["OwnerId"] == rule["OwnerId"]
                   and task["WhatId"] == episode["Id"] and task["NABD_Follow_Up_Type__c"] == "risk_warning"
                   and len(await self.tasks_for(episode["Id"])) == 1,
                   f"task {task and task['Id']} {task and task['Status']}")

    async def negative_warning(self) -> None:
        last = self.cases[-1]
        command, result = await self.follow_up(last, "follow-up-1", "very_negative")
        self.check("negative follow-up confirmed", result.status == "confirmed", f"{result.status} {result.reason or ''}")
        replay = await self.gateway.execute(command)
        streak = await self.admin.query(f"SELECT Negative_Streak__c FROM Case WHERE Id = {literal(last['case_id'])}")
        self.check("replaying the follow-up is not another contact",
                   replay.replayed and int(streak[0]["Negative_Streak__c"] or 0) == 2,
                   f"replayed={replay.replayed}, streak {streak[0]['Negative_Streak__c']}")

        async def opened() -> bool:
            return len(await self.episodes(NEGATIVE)) == 1
        found = await self.wait(opened)
        episodes = await self.episodes(NEGATIVE)
        if not self.check("persistent-negative warning opened once", found and len(episodes) == 1,
                          f"{len(episodes)} active"):
            return
        rows = [row for row in episodes[0].get("Risk_Evidence__r") or [] if row["Current__c"]]
        cited = [row for row in rows if row["Complaint_Evidence__c"]]
        self.check("sentiment warning cites the case and both negative contacts",
                   len(rows) == 3 and len(cited) == 2 and last["case_number"] in (episodes[0]["Explanation__c"] or ""),
                   f"{episodes[0]['Name']}: {len(rows)} rows, {len(cited)} customer evidence")

    async def stays_single(self) -> None:
        before = {code: await self.episodes(code) for code in (REPEAT, NEGATIVE)}
        _, result = await self.follow_up(self.cases[-1], "follow-up-2", "negative")
        await asyncio.sleep(10)  # give the queued evaluation time to run

        async def settled() -> bool:
            rows = await self.admin.query(
                "SELECT Id FROM AsyncApexJob WHERE ApexClass.Name = 'NabdRiskEvaluation' "
                "AND Status IN ('Queued', 'Preparing', 'Processing', 'Holding') LIMIT 1")
            return not rows
        await self.wait(settled)
        after = {code: await self.episodes(code) for code in (REPEAT, NEGATIVE)}
        same = all(len(after[code]) == 1 and after[code][0]["Id"] == before[code][0]["Id"]
                   for code in (REPEAT, NEGATIVE) if before[code])
        tasks = [len(await self.tasks_for(after[code][0]["Id"])) for code in (REPEAT, NEGATIVE) if after[code]]
        self.check("re-evaluation keeps one warning and one task per rule",
                   result.status == "confirmed" and same and tasks == [1, 1], f"tasks per warning {tasks}")

    async def close_as_duplicates(self) -> None:
        records = [{"Id": case["case_id"], "Status": "Closed", "Resolution_Outcome__c": "duplicate"}
                   for case in self.cases if case.get("case_id")]
        if not records:
            return
        results = await self.admin.update("Case", records)
        self.check("staff closed the smoke complaints as duplicates",
                   all(item.get("success") for item in results), f"{len(records)} cases")

    async def cleared(self, history: dict[str, int]) -> None:
        async def done() -> bool:
            return not await self.episodes(REPEAT) and not await self.episodes(NEGATIVE)
        found = await self.wait(done)
        details = []
        tasks_closed = True
        kept = True
        for code in (REPEAT, NEGATIVE):
            rows = await self.episodes(code, active_only=False)
            latest = rows[-1] if len(rows) > history[code] else None
            if latest is None:
                kept = False
                continue
            task = await self.task(latest["Follow_Up_Task_Id__c"])
            tasks_closed &= bool(task and task["IsClosed"] and "NABD cleared this warning" in (task["Description"] or ""))
            kept &= latest["Status__c"] == "cleared" and bool(latest.get("Risk_Evidence__r")) and not any(
                row["Current__c"] for row in latest["Risk_Evidence__r"])
            details.append(f"{latest['Name']} {latest['Status__c']}: {latest['Clear_Reason__c']}")
        self.check("both warnings cleared with a reason, evidence kept", found and kept, "; ".join(details))
        self.check("their open tasks were completed with a note", tasks_closed)
        totals = {code: len(await self.episodes(code, active_only=False)) for code in (REPEAT, NEGATIVE)}
        self.check("history keeps every earlier warning (recurrence opens a new one)",
                   all(totals[code] == history[code] + 1 for code in totals),
                   ", ".join(f"{code} {count}" for code, count in totals.items()))


async def run(target_org: str, settings: SalesforceSettings) -> list[Result]:
    cli = CliOrgAuth(target_org)
    async with httpx.AsyncClient(timeout=60) as http:
        admin = SalesforceClient(cli, http, s.API_VERSION)
        auth = ClientCredentialsAuth(settings.domain_url, settings.client_id, settings.client_secret, http)
        integration = SalesforceClient(auth, http, settings.api_version)
        smoke = RiskSmoke(admin, integration)
        try:
            return await smoke.run()
        except Exception as error:  # report, never print credentials
            smoke.check("risk smoke completed", False, f"{type(error).__name__}: {error}")
            return smoke.results

