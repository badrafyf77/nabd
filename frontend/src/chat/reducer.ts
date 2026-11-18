// Builds the visible conversation from a snapshot plus replayed or live events.
// Events are applied at most once (by sequence); a jump in sequence marks a gap,
// and the page then reloads the snapshot instead of guessing what was missed.

import type { Activity, ConversationState, RunView, ServerEvent, Snapshot } from "./types";

export function emptyState(): ConversationState {
  return { lastSequence: 0, messages: [], runs: {}, runOrder: [], activities: {}, partial: {}, gap: false };
}

function upsertRun(state: ConversationState, id: string, patch: Partial<RunView>): void {
  const current = state.runs[id];
  if (!current) {
    state.runOrder = [...state.runOrder, id];
  }
  const base: RunView = current ?? { id, messageId: "", status: "queued", attempt: 0 };
  state.runs = { ...state.runs, [id]: { ...base, ...patch, id } };
}

function applyOne(state: ConversationState, event: ServerEvent): void {
  const run = event.run_id;
  const p = event.payload || {};
  switch (event.type) {
    case "message.created":
      if (!state.messages.some((m) => m.id === p.message_id)) {
        state.messages = [...state.messages, { id: p.message_id, role: "customer", runId: run, text: p.text }];
      }
      if (run) upsertRun(state, run, { messageId: p.message_id });
      break;
    case "run.queued":
      if (run) upsertRun(state, run, { status: "queued", ahead: p.ahead, messageId: p.message_id });
      break;
    case "run.retrying":
      if (run) {
        upsertRun(state, run, { status: "retrying", attempt: p.attempt });
        const { [run]: _dropped, ...rest } = state.partial;
        state.partial = rest; // text of the abandoned attempt is discarded
      }
      break;
    case "run.started":
      // Keep the first start: a retried run's total time is what the customer waited.
      if (run) upsertRun(state, run, { status: "running", attempt: p.attempt,
                                       startedAt: state.runs[run]?.startedAt ?? event.created_at });
      break;
    case "message.delta": {
      if (!run) break;
      const current = state.partial[run];
      if (current && current.attempt > p.attempt) break;
      const base = current && current.attempt === p.attempt ? current.text : "";
      state.partial = { ...state.partial, [run]: { messageId: p.message_id, attempt: p.attempt, text: base + p.text } };
      break;
    }
    case "message.completed": {
      const message = { id: p.message_id, role: "assistant" as const, runId: run, text: p.text };
      state.messages = state.messages.some((m) => m.id === p.message_id)
        ? state.messages.map((m) => (m.id === p.message_id ? message : m))
        : [...state.messages, message];
      if (run) {
        const { [run]: _done, ...rest } = state.partial;
        state.partial = rest;
      }
      break;
    }
    case "run.completed":
    case "run.failed":
      if (run) upsertRun(state, run, {
        status: event.type === "run.completed" ? "completed" : "failed",
        error: p.error ?? null, endedAt: event.created_at,
      });
      break;
    case "activity": {
      if (!event.activity_id || !run || !event.state || !event.code) break;
      const previous = state.activities[event.activity_id];
      const finished = event.state === "completed" || event.state === "failed";
      const activity: Activity = {
        id: event.activity_id,
        parentId: event.parent_activity_id,
        runId: run,
        code: event.code,
        state: event.state,
        params: p.params || previous?.params || {},
        result: p.result ?? (event.state === "completed" ? previous?.result : undefined),
        error: p.error,
        waitingFor: event.state === "waiting" ? p.waiting_for : undefined,
        waitingOn: event.state === "waiting" ? p.waiting_on : undefined,
        retry: event.state === "retrying" ? p.retry : previous?.retry,
        startedAt: previous?.startedAt ?? event.created_at,
        endedAt: finished ? event.created_at : undefined,
        durationMs: finished ? p.duration_ms : undefined,
        order: previous?.order ?? event.sequence,
      };
      state.activities = { ...state.activities, [event.activity_id]: activity };
      break;
    }
  }
}

export function applyEvents(previous: ConversationState, events: ServerEvent[]): ConversationState {
  const state = { ...previous };
  for (const event of events) {
    if (event.sequence <= state.lastSequence) continue; // already applied (replay overlap)
    if (event.sequence !== state.lastSequence + 1) {
      state.gap = true;
      return state;
    }
    applyOne(state, event);
    state.lastSequence = event.sequence;
  }
  return state;
}

export function fromSnapshot(snapshot: Snapshot): ConversationState {
  const state = emptyState();
  state.messages = snapshot.messages.map((m) => ({ id: m.id, role: m.role, runId: m.run_id, text: m.text }));
  for (const run of snapshot.runs) {
    upsertRun(state, run.id, { messageId: run.message_id, status: run.status, attempt: run.attempt, error: run.error });
  }
  // Rebuild activity and timing from the lifecycle events, then trust the stored run status.
  for (const event of snapshot.events) {
    if (event.type !== "message.created" && event.type !== "message.completed") applyOne(state, event);
  }
  for (const run of snapshot.runs) {
    upsertRun(state, run.id, { status: run.status, attempt: run.attempt, error: run.error });
  }
  for (const [messageId, item] of Object.entries(snapshot.partial)) {
    state.partial = { ...state.partial, [item.run_id]: { messageId, attempt: state.runs[item.run_id]?.attempt ?? 1,
                                                         text: item.text } };
  }
  state.lastSequence = snapshot.last_sequence;
  return state;
}
