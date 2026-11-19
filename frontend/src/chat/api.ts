// Same-origin calls to the NABD API (proxied by next.config.mjs). The session is an
// HttpOnly cookie, so the browser never sees or stores the customer's identity token.

import type { ConversationSummary, DemoCustomer, Session, Snapshot } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string) {
    super(code);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}) },
  });
  if (!response.ok) {
    let code = `http_${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") code = body.detail;
    } catch {}
    throw new ApiError(response.status, code);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export const api = {
  customers: () => request<{ customers: DemoCustomer[] }>("/api/demo/customers"),
  session: () => request<Session>("/api/demo/session"),
  signIn: (customerId: string) =>
    request<Session>("/api/demo/session", { method: "POST", body: JSON.stringify({ customer_id: customerId }) }),
  signOut: () => request<void>("/api/demo/session", { method: "DELETE" }),
  conversations: () => request<{ conversations: ConversationSummary[] }>("/api/conversations"),
  createConversation: () => request<ConversationSummary>("/api/conversations", { method: "POST" }),
  snapshot: (id: string) => request<Snapshot>(`/api/conversations/${id}`),
  send: (id: string, clientMessageId: string, text: string) =>
    request<{ message_id: string; run_id: string; status: string }>(`/api/conversations/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ client_message_id: clientMessageId, text }),
    }),
};

export const EVENT_TYPES = [
  "staff.reply", "staff.reply.withdrawn", "case.staff_update", "case.removed",
  "conversation.language",
  "message.created", "message.delta", "message.completed", "run.queued", "run.started", "run.retrying",
  "run.completed", "run.failed", "activity",
];
