"""What one conversation should be shown of one case after staff changed it (Phase 5).

Pure decisions, no I/O. Inputs: what the conversation was last shown (its
issue link and delivered staff replies) and the case's current authorized
state from Salesforce. Output: the changes to store and the customer events to
append, all in one transaction. Because the result is a difference, the same
Salesforce change seen twice (duplicate, replayed or reconciled) adds nothing.

Rules:

* A case that is gone, or now belongs to another customer, is removed from the
  conversation together with every staff reply it showed (privacy).
* A status or staff-action change is announced unless the current version is
  one NABD's own confirmed command produced (NABD already showed it).
* A public comment posted after the conversation linked the case becomes a
  staff reply; an edit raises its revision; a comment that is no longer public
  (or was deleted) is withdrawn. Private notes are never part of the input.
* Translation is requested only when the conversation's reply language is not
  English (staff write English); the translator may still find none is needed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nabd.runtime.events import NewEvent
from nabd.salesforce.reads import CaseState

STAFF_REPLY = "staff.reply"
STAFF_REPLY_WITHDRAWN = "staff.reply.withdrawn"
CASE_STAFF_UPDATE = "case.staff_update"
CASE_REMOVED = "case.removed"


@dataclass(frozen=True)
class ShownLink:
    """``nabd.issue_links`` for one conversation and case, plus the conversation's customer."""

    conversation_id: str
    case_id: str
    customer_id: str
    case_number: str
    status: str | None
    staff_action: str | None
    staff_action_completed: bool | None
    removed: bool
    synced: bool
    linked_at: datetime


@dataclass(frozen=True)
class ShownReply:
    id: str
    comment_id: str
    body: str
    revision: int
    posted_at: datetime
    withdrawn: bool
    translation_status: str
    translation: dict[str, Any] | None
    target_language: dict[str, Any] | None


@dataclass
class Plan:
    link: dict[str, Any] = field(default_factory=dict)  # issue_links columns to set
    new_replies: list[dict[str, Any]] = field(default_factory=list)
    revised_replies: list[dict[str, Any]] = field(default_factory=list)  # edited or restored
    withdrawn: list[str] = field(default_factory=list)  # staff_replies IDs
    events: list[NewEvent] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.link or self.new_replies or self.revised_replies or self.withdrawn or self.events)


def normalized_version(value: Any) -> str | None:
    """Salesforce versions in one comparable form (the gateway and REST format them differently)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return text
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reply_payload(reply_id: str, case_number: str, body: str, posted_at: datetime, revision: int,
                  translation_status: str, translation: dict[str, Any] | None = None) -> dict[str, Any]:
    """The public ``staff.reply`` payload. Original text always; a translation only when done."""
    item: dict[str, Any] = {"status": translation_status}
    if translation_status == "done" and translation:
        item.update({key: translation.get(key) for key in ("text", "reply_language", "reply_script",
                                                            "source_language")})
    return {"reply_id": reply_id, "case_number": case_number, "text": body, "posted_at": posted_at.isoformat(),
            "revision": revision, "translation": item}


def _translation_status(target: dict[str, Any] | None) -> str:
    return "pending" if target and target.get("reply_language") not in (None, "en") else "none"


def plan_case_view(link: ShownLink, replies: list[ShownReply], state: CaseState | None,
                   own_versions: set[str], target_language: dict[str, Any] | None, now: datetime) -> Plan:
    plan = Plan()
    visible = state is not None and state.customer_id == link.customer_id
    active = [reply for reply in replies if not reply.withdrawn]
    if not visible:
        if not link.removed:
            plan.link.update(removed_at=now, synced_at=now)
            for reply in active:
                plan.withdrawn.append(reply.id)
                plan.events.append(NewEvent(type=STAFF_REPLY_WITHDRAWN, code=STAFF_REPLY_WITHDRAWN,
                                            payload={"reply_id": reply.id, "case_number": link.case_number}))
            plan.events.append(NewEvent(type=CASE_REMOVED, code=CASE_REMOVED,
                                        payload={"case_number": link.case_number}))
        return plan

    assert state is not None
    plan.link["synced_at"] = now
    changes: list[str] = []
    own = normalized_version(state.record.version) in own_versions
    if link.removed:
        plan.link["removed_at"] = None
        changes.append("restored")
    if state.case_number != link.case_number:
        plan.link["case_number"] = state.case_number
    if state.status != link.status:
        plan.link["status"] = state.status
        if not own:
            changes.append("status")
    if state.staff_action != link.staff_action or state.staff_action_completed != link.staff_action_completed:
        plan.link.update(staff_action=state.staff_action, staff_action_completed=state.staff_action_completed)
        completed_now = state.staff_action_completed and link.staff_action_completed is False
        if not own and (link.synced or completed_now):
            changes.append("staff_action_completed" if completed_now else "staff_action_changed")
    if changes:
        plan.events.append(NewEvent(type=CASE_STAFF_UPDATE, code=CASE_STAFF_UPDATE, payload={
            "case_number": state.case_number, "status": state.status,
            "previous_status": link.status if "status" in changes else None,
            "staff_action": state.staff_action, "staff_action_completed": state.staff_action_completed,
            "changes": changes,
        }))

    shown = {reply.comment_id: reply for reply in replies}
    public = {reply.comment_id: reply for reply in state.replies if reply.posted_at >= link.linked_at}
    for comment_id, comment in public.items():
        existing = shown.get(comment_id)
        if existing is None:
            reply_id = str(uuid.uuid4())
            status = _translation_status(target_language)
            plan.new_replies.append({"id": reply_id, "comment_id": comment_id, "body": comment.body,
                                     "posted_at": comment.posted_at, "translation_status": status,
                                     "target_language": target_language if status == "pending" else None})
            plan.events.append(NewEvent(type=STAFF_REPLY, code=STAFF_REPLY, payload=reply_payload(
                reply_id, state.case_number, comment.body, comment.posted_at, 1, status)))
        elif existing.withdrawn or existing.body != comment.body:
            revision = existing.revision + 1
            status = _translation_status(target_language)
            plan.revised_replies.append({"id": existing.id, "body": comment.body, "revision": revision,
                                         "translation_status": status,
                                         "target_language": target_language if status == "pending" else None})
            plan.events.append(NewEvent(type=STAFF_REPLY, code=STAFF_REPLY, payload=reply_payload(
                existing.id, state.case_number, comment.body, comment.posted_at, revision, status)))
    for reply in active:
        if reply.comment_id not in public:
            plan.withdrawn.append(reply.id)
            plan.events.append(NewEvent(type=STAFF_REPLY_WITHDRAWN, code=STAFF_REPLY_WITHDRAWN,
                                        payload={"reply_id": reply.id, "case_number": state.case_number}))
    return plan
