"""The validated case workflow: code that turns a verified complaint assessment into case commands.

The Complaint specialist interprets; this module decides. It applies the
business rules (routing and review, priority policy, lifecycle), builds one
typed command per case, stores the plan and all its commands in PostgreSQL
before anything is sent, and only then calls the write MCP server.

Reliability rules:

* a run's plan is saved once; a retried run reuses it, so operation IDs,
  issue IDs and evidence keys never change and Salesforce replays receipts;
* an ambiguous result (timeout, transport error) is looked up by operation ID
  before the same command is sent again;
* a version conflict re-reads the case once and resends only if the same
  action is still valid for the case's new state;
* case mutations of one customer run under a PostgreSQL advisory lock, so a
  concurrent run of the same customer sees this run's new case and can match it.

Customer activity cards show a case number and team only after Salesforce
confirmed the operation. Internal assessments never reach the cards.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel

from nabd.agents.complaints import (
    Assessment, Candidate, ChatLine, ComplaintSpecialist, Issue, verify_assessment,
)
from nabd.agents.language import original_script
from nabd.agents.mcp_tools import McpTools, tool_activity
from nabd.runtime.events import Activity, NewEvent, RunContext
from nabd.runtime.store import CommandRow, NewCommand
from nabd.salesforce import schema as s
from nabd.salesforce.commands import CommandResult, salesforce_time

TEAMS = {queue.developer_name: queue.label for queue in s.QUEUES}
WRITE_TOOLS = {"create_case": "create_case", "update_case": "update_case", "reopen_case": "reopen_case",
               "request_closure": "request_case_closure", "close_case": "close_case"}
AMBIGUOUS = {"unavailable", "transport"}
MAX_EVIDENCE = 20

# Priority policy (architecture defaults). Reported signals count: staff verify them.
HIGH_SIGNALS = {"safety_incident": "reported safety incident", "security_incident": "reported security incident",
                "unauthorized_charge": "suspected unauthorized charge",
                "duplicate_charge": "suspected duplicate charge"}
MEDIUM_SIGNALS = {"lost_parcel": "lost parcel", "repeated_unresolved": "repeated unresolved issue"}


def priority_for(signals: list[str] | set[str], *, reopening: bool = False) -> tuple[str, str]:
    """(priority, reason) from explicit rules; the model only reports the signals."""
    present = set(signals) | ({"repeated_unresolved"} if reopening else set())
    for name, text in HIGH_SIGNALS.items():
        if name in present:
            return "High", f"High: {text}"
    for name, text in MEDIUM_SIGNALS.items():
        if name in present:
            return "Medium", f"Medium: {text}"
    return "Low", "Low: no stronger signal"


def route(category: str, confidence: float, threshold: float) -> tuple[str, bool]:
    """(category, needs review). Uncertain classifications go to General Support for staff review."""
    if confidence < threshold:
        return "General_Support", True
    return category, False


def resolve_operation(issue: Issue, candidate: Candidate | None) -> str | None:
    """The lifecycle-valid operation for a proposal, or None when nothing should be written."""
    if issue.match == "unsure" or issue.action in ("gather_information", "no_action"):
        return None
    if candidate is None:
        return "create_case" if issue.action in ("create", "update", "reopen") else None
    if candidate.is_closed:
        # The same problem came back after resolution: reopen. A closed case cannot be closed again.
        return "reopen_case" if issue.action in ("create", "update", "reopen") else None
    if issue.action in ("close", "request_closure"):
        if candidate.staff_action_pending or not issue.facts:
            return "update_case"  # record what the customer said; staff or evidence must come first
        if issue.action == "close" and issue.confirmation_message_id:
            return "close_case"
        return "request_closure"
    return "update_case"


def parse_candidates(case_history: dict[str, Any]) -> dict[str, Candidate]:
    """Case number -> candidate, with the record ID and version from each evidence reference."""
    candidates = {}
    for item in case_history.get("cases", []):
        ref = next((r for r in item.get("evidence", []) if r.startswith("Case:")), None)
        if not ref or "@" not in ref:
            continue
        record_id, stamp = ref[len("Case:"):].split("@", 1)
        candidates[item["case_number"]] = Candidate(
            case_number=item["case_number"], case_id=record_id,
            version=salesforce_time(datetime.fromisoformat(stamp)), status=item.get("status") or "",
            is_closed=item.get("status") == "Closed", staff_action=item.get("staff_action"),
            staff_action_completed=bool(item.get("staff_action_completed")),
            order_number=item.get("order_number"), subject=item.get("subject"),
            reopened_times=int(item.get("reopened_times") or 0))
    return candidates


def operation_id(run_id: str, position: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nabd:case-op:{run_id}:{position}"))


def issue_id(run_id: str, ref: str) -> str:
    return "nabd-issue-" + uuid.uuid5(uuid.NAMESPACE_URL, f"nabd:issue:{run_id}:{ref}").hex


def _fact_ref(issue: Issue, statement: str, evidence: list[str]) -> str:
    digest = hashlib.sha256(f"{'|'.join(sorted(evidence))}|{statement}".encode()).hexdigest()[:24]
    return f"fact-{digest}"


def _evidence(issue: Issue, operation: str, messages: dict[str, ChatLine], at: datetime,
              message_languages: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message_id in issue.message_ids:
        line = messages.get(message_id)
        if line is None:
            continue
        kind = ("customer_confirmation" if operation == "close_case" and message_id == issue.confirmation_message_id
                else "customer_report")
        items.append({"ref": message_id, "kind": kind, "text": line.text,
                      "language": message_languages.get(message_id), "script": original_script(line.text),
                      "captured_at": salesforce_time(line.at or at)})
    if operation == "close_case" and issue.confirmation_message_id not in issue.message_ids:
        line = messages[issue.confirmation_message_id]
        items.append({"ref": line.message_id, "kind": "customer_confirmation", "text": line.text,
                      "language": message_languages.get(line.message_id), "script": original_script(line.text),
                      "captured_at": salesforce_time(line.at or at)})
    for fact in issue.facts:
        items.append({"ref": _fact_ref(issue, fact.statement, fact.evidence), "kind": "verified_fact",
                      "text": fact.statement, "language": "en", "script": "latin",
                      "verified_source": fact.evidence[0][:255],
                      "captured_at": salesforce_time(at)})
    return [{k: v for k, v in item.items() if v is not None} for item in items[:MAX_EVIDENCE]]


@dataclass
class Plan:
    commands: list[NewCommand] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def command_for(run_id: str, position: str, issue: Issue, operation: str, candidate: Candidate | None,
                messages: dict[str, ChatLine], at: datetime, *, lost_orders: set[str],
                review_threshold: float, message_languages: dict[str, str] | None = None) -> NewCommand:
    """One typed case command for a verified issue (see ``resolve_operation``)."""
    signals = set(issue.signals) | ({"lost_parcel"} if set(issue.order_numbers) & lost_orders else set())
    priority, reason = priority_for(signals, reopening=operation == "reopen_case")
    evidence = _evidence(issue, operation, messages, at, message_languages or {})
    card = {"operation": operation, "title": issue.title,
            "case_number": candidate.case_number if candidate else None}
    if operation == "create_case":
        category, review = route(issue.category, issue.category_confidence, review_threshold)
        request = {
            "issue_id": issue_id(run_id, issue.ref), "category": category, "subject": issue.title,
            "summary": issue.summary, "priority": priority, "priority_reason": reason,
            "sentiment": issue.sentiment, "urgency": issue.urgency,
            "confidence": round(issue.category_confidence, 3), "needs_review": review, "language": issue.language,
            "evidence": evidence, "order_number": issue.order_numbers[0] if issue.order_numbers else None,
            "staff_action": issue.staff_action,
        }
    else:
        assert candidate is not None
        request = {"case_id": candidate.case_id, "expected_version": candidate.version, "evidence": evidence,
                   "sentiment": issue.sentiment}
        if operation in ("update_case", "reopen_case"):
            request.update({"summary": issue.summary, "urgency": issue.urgency, "priority": priority,
                            "priority_reason": reason, "staff_action": issue.staff_action})
        if operation == "close_case":
            request["confirmation_message_id"] = issue.confirmation_message_id
    operation_key = operation_id(run_id, position)
    # Exactly the write tool's arguments, stored before sending so every retry sends the same request.
    request = {"operation_id": operation_key, **{key: value for key, value in request.items() if value is not None}}
    return NewCommand(operation_key, position, issue.ref, operation, request, card)


def build_plan(run_id: str, assessment: Assessment, candidates: dict[str, Candidate],
               messages: dict[str, ChatLine], at: datetime, *, lost_orders: set[str],
               review_threshold: float) -> Plan:
    plan = Plan()
    if assessment.outcome == "needs_clarification" and assessment.question:
        plan.questions.append(assessment.question)
    touched: set[str] = set()
    for issue in assessment.issues:
        candidate = candidates.get(issue.existing_case) if issue.existing_case else None
        if issue.match == "unsure" or issue.action == "gather_information":
            plan.questions.append(issue.question or (
                f"Is this about your existing case {issue.existing_case}, or a new problem?" if issue.existing_case
                else "Could you tell me a little more about this problem?"))
            continue
        operation = resolve_operation(issue, candidate)
        if operation is None:
            if candidate and candidate.is_closed and issue.action in ("close", "request_closure"):
                plan.notes.append(f"Case {candidate.case_number} is already closed.")
            continue
        if candidate and candidate.case_number in touched:
            continue  # one command per case per turn
        if candidate and issue.action in ("close", "request_closure") and operation == "update_case":
            plan.notes.append(
                f"Case {candidate.case_number} was not closed: "
                + ("staff still have to complete the required action." if candidate.staff_action_pending
                   else "no checked record shows the problem is solved."))
        if candidate:
            touched.add(candidate.case_number)
        plan.commands.append(command_for(run_id, str(len(plan.commands) + 1), issue, operation, candidate,
                                         messages, at, lost_orders=lost_orders, review_threshold=review_threshold,
                                         message_languages=assessment.message_languages))
    return plan


class CaseStore(Protocol):
    def case_lock(self, customer_id: str, on_wait: Any = None) -> Any: ...
    async def case_plan(self, run_id: str) -> tuple[dict[str, Any] | None, list[CommandRow]]: ...
    async def save_case_plan(self, run_id: str, conversation_id: str, customer_id: str,
                             assessment: dict[str, Any], commands: list[NewCommand]) -> bool: ...
    async def add_command(self, run_id: str, conversation_id: str, customer_id: str, command: NewCommand) -> None: ...
    async def mark_command(self, operation_id: str, status: str, *, result: dict[str, Any] | None = None,
                           error: str | None = None, sent: bool = False) -> None: ...
    async def record_case(self, conversation_id: str, customer_id: str, run_id: str, *, case_id: str,
                          case_number: str, department: str | None, status: str | None, operation: str) -> None: ...


def _short(operation: str) -> str:
    return {"create_case": "create", "update_case": "update", "reopen_case": "reopen",
            "request_closure": "request_closure", "close_case": "close"}[operation]


def public_result(row: CommandRow | NewCommand, result: CommandResult | None) -> dict[str, Any]:
    """The command's outcome as the Supervisor and the customer card may know it."""
    card = dict(row.card)
    # An existing case's number is already known; a new case gets its number and team only once confirmed.
    entry = {"operation": row.operation, "title": card.get("title"), "case_number": card.get("case_number"),
             "team": None, "status": "pending"}
    if result is not None:
        entry.update(status=result.status, reason=result.reason, case_status=result.case_status)
        if result.case_number:
            entry["case_number"] = result.case_number
        if result.queue:
            entry["team"] = TEAMS.get(result.queue)  # None: a staff member owns the case now
    return entry


def _card_result(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in ("case_number", "team", "case_status", "operation")}


@dataclass
class ComplaintStage:
    """Runs the mandatory complaint stage of one turn (see the module docstring)."""

    model: BaseChatModel
    store: CaseStore
    review_threshold: float = 0.6

    async def run(self, run: RunContext, turn: Any, reads: McpTools, writes: McpTools) -> dict[str, Any]:
        me = run.activity("complaints", "agent.complaints")
        await me.start()

        async def waiting() -> None:
            await me.wait("dependency", reason="other_conversation")

        async with self.store.case_lock(run.customer_id, on_wait=waiting) as waited:
            if waited:
                await me.resume()
            stored, rows = await self.store.case_plan(run.run_id)
            if stored is None:
                stored = await self._plan(run, turn, reads, me)
                rows = (await self.store.case_plan(run.run_id))[1]
            assessment = Assessment.from_json(stored["assessment"])
            if assessment.language:
                await run.emit(NewEvent(type="conversation.language", payload=assessment.language))
            results = []
            for row in rows:
                if row.position.endswith("r"):
                    continue  # a conflict retry; handled with its original
                results.append(await self._execute(run, row, rows, stored, reads, writes, me))
        await me.complete(**assessment.public())
        return {
            "outcome": assessment.outcome,
            "language": assessment.language,
            "questions": stored.get("questions", []),
            "notes": stored.get("notes", []),
            "facts": [fact.statement for issue in assessment.issues for fact in issue.facts],
            "cases": results,
        }

    async def _plan(self, run: RunContext, turn: Any, reads: McpTools, me: Activity) -> dict[str, Any]:
        history_call = {"id": "case-history", "name": "list_case_history", "args": {}}
        outcome = await reads.call(run, "list_case_history", {}, allowed=("list_case_history",),
                                   activity=tool_activity(run, history_call, me))
        if not outcome.ok:
            raise RuntimeError(f"case history unavailable ({outcome.error})")
        case_history = outcome.data
        candidates = parse_candidates(case_history)
        lines = list(turn.history) or [ChatLine("customer", turn.message_id, turn.text, turn.message_at)]
        current = next((line for line in lines if line.message_id == turn.message_id),
                       ChatLine("customer", turn.message_id, turn.text, turn.message_at))
        messages = {line.message_id: line for line in lines if line.role == "customer"}
        messages.setdefault(current.message_id, current)
        specialist = ComplaintSpecialist(self.model, reads, turn.today)
        found = await specialist.investigate(run, specialist.request(lines, current, case_history), me)
        assessment = verify_assessment(
            found.raw, evidence=found.evidence | outcome.evidence, orders_seen=found.orders_seen,
            candidates=candidates, customer_messages=messages, current_message_id=turn.message_id,
            tool_calls=found.tool_calls)
        at = current.at or datetime.now(timezone.utc)
        plan = build_plan(run.run_id, assessment, candidates, messages, at, lost_orders=found.lost_orders,
                          review_threshold=self.review_threshold)
        stored = {"assessment": assessment.to_json(), "questions": plan.questions, "notes": plan.notes,
                  "lost_orders": sorted(found.lost_orders),
                  "messages": {key: {"text": line.text, "at": line.at.isoformat() if line.at else None}
                               for key, line in messages.items()}}
        await self.store.save_case_plan(run.run_id, run.conversation_id, run.customer_id, stored, plan.commands)
        return (await self.store.case_plan(run.run_id))[0] or stored

    def card(self, run: RunContext, row: CommandRow) -> Activity:
        """The customer card of a command, outside its run (background reconciliation)."""
        return self._activity(run, row, None, parent_id=f"{row.run_id}:complaints")

    def _activity(self, run: RunContext, row: CommandRow | NewCommand, me: Activity | None,
                  parent_id: str | None = None) -> Activity:
        params = {"case_number": row.card.get("case_number")} if row.card.get("case_number") else {}
        activity = run.activity(f"case:{row.operation_id}", f"case.{_short(row.operation)}", parent=me,
                                params=params)
        if me is None and parent_id:
            activity.parent_id = parent_id
        return activity

    async def _execute(self, run: RunContext, row: CommandRow, rows: list[CommandRow], stored: dict[str, Any],
                       reads: McpTools, writes: McpTools, me: Activity) -> dict[str, Any]:
        activity = self._activity(run, row, me)
        retry = next((r for r in rows if r.position == row.position + "r"), None)
        if row.final and not (row.status == "conflict" and (row.result or {}).get("reason") == "version_changed"):
            result = CommandResult.model_validate(row.result) if row.result else None
            entry = public_result(row, result)
            if run.attempt > 1:  # an earlier attempt may have stopped before finishing the card
                await self._finish_card(activity, entry)
            return entry
        await activity.start()
        result = None
        if not row.final:
            await activity.wait("external_operation")
            result = await self._settle(row, writes)
            if result is None:
                return public_result(row, None)  # still unknown: background reconciliation finishes it
            await self._record(run.conversation_id, run.customer_id, run.run_id, row, result)
        else:
            result = CommandResult.model_validate(row.result)
        if result.status == "conflict" and result.reason == "version_changed":
            retry_row = retry or await self._replan(run, row, stored, reads)
            if retry_row is not None:
                await activity.retrying("version_changed", 1)
                if not retry_row.final:
                    await activity.wait("external_operation")
                    retried = await self._settle(retry_row, writes)
                    if retried is None:
                        return public_result(retry_row, None)
                    await self._record(run.conversation_id, run.customer_id, run.run_id, retry_row, retried)
                else:
                    retried = CommandResult.model_validate(retry_row.result)
                row, result = retry_row, retried
        entry = public_result(row, result)
        await self._finish_card(activity, entry)
        return entry

    async def _finish_card(self, activity: Activity, entry: dict[str, Any]) -> None:
        if entry["status"] == "confirmed":
            await activity.complete(**_card_result(entry))
        elif entry["status"] != "pending":
            await activity.fail(entry.get("reason") or entry["status"])

    async def _replan(self, run: RunContext, row: CommandRow, stored: dict[str, Any],
                      reads: McpTools) -> CommandRow | None:
        """After a version conflict: re-read the case and resend only if the same action still applies."""
        refreshed = await reads.call_raw("list_case_history", {})
        if not refreshed.ok:
            return None
        candidates = parse_candidates(refreshed.data)
        assessment = Assessment.from_json(stored["assessment"])
        issue = next((i for i in assessment.issues if i.ref == row.issue_ref), None)
        candidate = candidates.get(row.card.get("case_number") or "")
        if issue is None or candidate is None or resolve_operation(issue, candidate) != row.operation:
            return None
        messages = {key: ChatLine("customer", key, value["text"],
                                  datetime.fromisoformat(value["at"]) if value.get("at") else None)
                    for key, value in stored.get("messages", {}).items()}
        at = next((line.at for line in messages.values() if line.at), datetime.now(timezone.utc))
        command = command_for(run.run_id, row.position + "r", issue, row.operation, candidate, messages, at,
                              lost_orders=set(stored.get("lost_orders", [])), review_threshold=self.review_threshold,
                              message_languages=assessment.message_languages)
        await self.store.add_command(run.run_id, run.conversation_id, run.customer_id, command)
        rows = (await self.store.case_plan(run.run_id))[1]
        return next((r for r in rows if r.operation_id == command.operation_id), None)

    async def _settle(self, row: CommandRow, writes: McpTools) -> CommandResult | None:
        """Send (or, if it may already have run, look up first) one command until its outcome is known.

        Returns None when Salesforce still gave no definite answer; the command stays ``unknown``.
        """
        if row.status in ("dispatched", "unknown"):
            looked = await self._lookup(row, writes)
            if looked is None:
                return None
            if looked.status != "unknown":
                return looked
        for _ in range(2):  # the first send, then one resend after a lookup proved it never ran
            await self.store.mark_command(row.operation_id, "dispatched", sent=True)
            outcome = await writes.call_raw(WRITE_TOOLS[row.operation], row.request)
            if outcome.ok:
                return CommandResult.model_validate(outcome.data)
            if outcome.error not in AMBIGUOUS:
                return CommandResult(status="failed", reason=outcome.error or "failed",
                                     operation_id=row.operation_id, operation=row.operation)
            await self.store.mark_command(row.operation_id, "unknown", error=outcome.error)
            looked = await self._lookup(row, writes)
            if looked is None:
                return None
            if looked.status != "unknown":
                return looked
        return None

    async def _lookup(self, row: CommandRow, writes: McpTools) -> CommandResult | None:
        outcome = await writes.call_raw("get_operation", {"operation_id": row.operation_id})
        if not outcome.ok:
            return None
        return CommandResult.model_validate(outcome.data)

    async def _record(self, conversation_id: str, customer_id: str, run_id: str, row: CommandRow,
                      result: CommandResult) -> None:
        await self.store.mark_command(row.operation_id, result.status, result=result.model_dump(mode="json"),
                                      error=result.reason if result.status != "confirmed" else None)
        if result.status == "confirmed" and result.case_id and result.case_number:
            await self.store.record_case(
                conversation_id, customer_id, run_id, case_id=result.case_id, case_number=result.case_number,
                department=TEAMS.get(result.queue or ""), status=result.case_status, operation=row.operation)

    async def reconcile(self, run: RunContext, row: CommandRow, writes: McpTools) -> CommandResult | None:
        """Finish a command whose run ended before Salesforce gave a definite answer."""
        result = await self._settle(row, writes)
        if result is None:
            return None
        await self._record(row.conversation_id, row.customer_id, row.run_id, row, result)
        await self._finish_card(self.card(run, row), public_result(row, result))
        return result
