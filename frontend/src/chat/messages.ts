import type { Activity, RunView } from "./types";
import { ref, statusText, t, teamText, type Locale, type TextKey } from "./i18n";
const ERRORS: Record<string, TextKey> = {
  unavailable: "errorUnavailable", transport: "errorUnavailable", not_allowed: "errorForbidden", unauthorized: "errorForbidden",
  invalid_argument: "errorInvalid", invalid_command: "errorInvalid", operation_reused: "errorInvalid", not_found: "errorMissing",
  order_not_found: "errorMissing", case_not_found: "errorMissing", limit_reached: "errorLimit", version_changed: "errorVersion",
  case_closed: "errorClosed", case_open: "errorOpen", staff_action_pending: "errorStaff", closed_as_duplicate: "errorDuplicate",
  abandoned: "errorStopped", reconcile_exhausted: "errorUnconfirmed",
};
export function errorText(code: string | undefined, locale: Locale = "en"): string { return t(locale, ERRORS[code || ""] || "errorFailed"); }
export function activityTitle(a: Activity, locale: Locale = "en"): string {
  const done = a.state === "completed", failed = a.state === "failed", r = a.result || {};
  let key: TextKey = "working";
  switch (a.code) {
    case "agent.supervisor": key = a.state === "waiting" ? "supervisorWait" : done ? "supervisorDone" : "supervisorWork"; break;
    case "agent.orders": key = done ? "ordersDone" : "ordersWork"; break;
    case "tool.list_orders": key = done ? "ordersFound" : "ordersLook"; break;
    case "tool.get_order": key = done ? r.found ? "orderDone" : "orderMissing" : "orderCheck"; break;
    case "tool.get_knowledge": key = done ? "knowledgeRead" : "knowledgeWork"; break;
    case "tool.list_knowledge": key = done ? "indexDone" : "indexWork"; break;
    case "tool.list_cases": key = done ? "casesFound" : "casesLook"; break;
    case "tool.get_customer_profile": key = done ? "profileDone" : "profileWork"; break;
    case "agent.complaints": key = a.state === "waiting" ? "complaintsWait" : !done ? "complaintsWork"
      : r.outcome === "issues" ? "issues" : r.outcome === "needs_clarification" ? "clarification" : "noCase"; break;
    case "tool.list_case_history": key = done ? "historyDone" : "historyWork"; break;
    case "case.create": key = done ? "caseCreated" : failed ? "caseCreateFailed" : "caseCreate"; break;
    case "case.update": key = done ? "caseUpdated" : failed ? "caseUpdateFailed" : "caseUpdate"; break;
    case "case.reopen": key = done ? "caseReopened" : failed ? "caseReopenFailed" : "caseReopen"; break;
    case "case.request_closure": key = done ? "caseReady" : failed ? "caseUpdateFailed" : "caseCheck"; break;
    case "case.close": key = done ? "caseClosed" : failed ? "caseCloseFailed" : "caseClose"; break;
  }
  return t(locale, key, { count: r.issues ?? r.count ?? 0,
    number: ref(r.case_number || r.order_number || a.params.case_number || a.params.order_number || "") });
}
export function activityDetail(a: Activity, locale: Locale = "en"): string | null {
  if (a.state === "retrying") return `${errorText(a.error, locale)}. ${t(locale, "retryDetail", { count: a.retry ?? 1 })}`;
  if (a.state === "failed") return errorText(a.error, locale);
  if (a.state === "waiting") {
    if (a.waitingFor === "customer_input") return t(locale, "customerWait");
    if (a.waitingFor === "external_operation") return t(locale, "externalWait");
    if (a.waitingReason === "other_conversation") return t(locale, "otherWait");
  }
  return null;
}
export function outcomeText(outcome: string, locale: Locale): string {
  return t(locale, outcome === "done" ? "outcomeDone" : outcome === "needs_information" ? "outcomeNeeds" : "outcomeCannot");
}
export function runHeadline(run: RunView, locale: Locale = "en"): string {
  return t(locale, run.status === "queued" ? run.ahead ? "queuedAhead" : "queued" : run.status === "running" ? "runWork"
    : run.status === "retrying" ? "runRetry" : run.status === "completed" ? "runDone"
    : run.error === "deadline_exceeded" ? "runTimeout" : "runFailed", { count: run.attempt });
}
export function seconds(ms: number): string { return ms < 10_000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 1000)} s`; }
export function caseLine(r: Record<string, any>, locale: Locale = "en"): string {
  return [r.case_number && t(locale, "caseLabel", { number: ref(r.case_number) }), teamText(r.team, locale), statusText(r.case_status, locale)].filter(Boolean).join(" · ");
}
