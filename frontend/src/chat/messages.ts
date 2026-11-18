// English texts for activity and run codes sent by the backend. The backend sends
// stable codes and allowed parameters only; Phase 4 adds the other languages here.

import type { Activity, RunView } from "./types";

const ERRORS: Record<string, string> = {
  unavailable: "Salesforce didn't respond",
  transport: "The records service couldn't be reached",
  not_allowed: "That step isn't available to this assistant",
  invalid_argument: "That reference isn't valid",
  not_found: "Nothing was found",
  limit_reached: "Step limit reached",
  unauthorized: "Not authorised",
  failed: "This step failed",
};

export function errorText(code: string | undefined): string {
  return (code && ERRORS[code]) || ERRORS.failed;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function activityTitle(a: Activity): string {
  const done = a.state === "completed";
  const r = a.result || {};
  switch (a.code) {
    case "agent.supervisor":
      if (a.state === "waiting") return "Waiting for the Orders specialist";
      return done ? "Support assistant finished" : "Support assistant is working on your message";
    case "agent.orders":
      return done ? "Orders specialist finished checking" : "Orders specialist is checking your orders";
    case "tool.list_orders":
      return done ? `Found ${plural(r.count ?? 0, "order", "orders")} on your account` : "Looking up your orders";
    case "tool.get_order": {
      const number = a.params.order_number || "the order";
      if (!done) return `Checking order ${number}`;
      return r.found ? `Checked order ${r.order_number || number}` : `Order ${number} isn't on your account`;
    }
    case "tool.get_knowledge":
      return done ? "Read ShopEasy help articles" : "Reading ShopEasy help articles";
    case "tool.list_knowledge":
      return done ? "Read the help index" : "Reading the help index";
    case "tool.list_cases":
      return done ? `Found ${plural(r.count ?? 0, "support case", "support cases")}` : "Looking up your support cases";
    case "tool.get_customer_profile":
      return done ? "Checked your account details" : "Checking your account details";
    default:
      return "Working";
  }
}

export function activityDetail(a: Activity): string | null {
  if (a.state === "retrying") return `${errorText(a.error)}. Trying again (retry ${a.retry ?? 1}).`;
  if (a.state === "failed") return errorText(a.error);
  if (a.state === "waiting") {
    if (a.waitingFor === "customer_input") return "Waiting for your answer";
    if (a.waitingFor === "external_operation") return "Waiting for Salesforce to confirm";
    return null;
  }
  return null;
}

export const OUTCOME: Record<string, string> = {
  done: "Answer found",
  needs_information: "Needs more information from you",
  cannot_complete: "Couldn't complete this check",
};

export function runHeadline(run: RunView): string {
  switch (run.status) {
    case "queued":
      return run.ahead ? "Queued: I'll answer after your previous message" : "Queued";
    case "running":
      return "Working on your reply";
    case "retrying":
      return `Something went wrong; trying again (attempt ${run.attempt})`;
    case "completed":
      return "Reply ready";
    case "failed":
      if (run.error === "deadline_exceeded") return "This took too long, so I stopped. Please try again.";
      if (run.error === "attempts_exhausted") return "I couldn't finish this reply after several tries. Please try again.";
      return "I couldn't finish this reply. Please try again.";
  }
}

export function seconds(ms: number): string {
  return ms < 10_000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 1000)} s`;
}
