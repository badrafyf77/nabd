// Shapes of the NABD chat API (backend/code/nabd/api.py and runtime/store.py).

export type ActivityState = "queued" | "running" | "waiting" | "retrying" | "completed" | "failed";
export type RunStatus = "queued" | "running" | "retrying" | "completed" | "failed";

export type DemoCustomer = {
  customer_id: string;
  first_name: string | null;
  last_name: string;
  preferred_language: string | null;
};

export type Session = {
  customer_id: string;
  first_name: string;
  display_name: string;
  expires_at: string;
};

export type ConversationSummary = {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  last_sequence: number;
};

export type ServerEvent = {
  id: string;
  sequence: number;
  conversation_id: string;
  run_id: string | null;
  type: string;
  activity_id: string | null;
  parent_activity_id: string | null;
  state: ActivityState | null;
  code: string | null;
  payload: Record<string, any>;
  created_at: string;
};

export type Snapshot = {
  language?: import("./i18n").TurnLanguage | null;
  conversation: ConversationSummary;
  last_sequence: number;
  messages: { id: string; role: "customer" | "assistant"; run_id: string | null; text: string; created_at: string }[];
  runs: { id: string; message_id: string; turn: number; status: RunStatus; attempt: number; error: string | null;
          created_at: string }[];
  events: ServerEvent[];
  partial: Record<string, { run_id: string; text: string }>;
  cases: CaseLink[];
  staff_replies?: StaffReply[];
  staff_notices?: ServerEvent[];
};

// A case this conversation opened or changed, as Salesforce last confirmed it.
export type CaseLink = {
  case_number: string;
  department: string | null;
  status: string | null;
  last_operation: string;
};

export type Activity = {
  id: string;
  parentId: string | null;
  runId: string;
  code: string;
  state: ActivityState;
  params: Record<string, any>;
  result?: Record<string, any>;
  error?: string;
  waitingFor?: string;
  waitingOn?: string | null;
  waitingReason?: string;
  retry?: number;
  startedAt: string;
  endedAt?: string;
  durationMs?: number;
  order: number;
};

export type RunView = {
  language?: import("./i18n").TurnLanguage;
  id: string;
  messageId: string;
  status: RunStatus;
  attempt: number;
  ahead?: number;
  error?: string | null;
  startedAt?: string;
  endedAt?: string;
};

export type ChatMessage = {
  id: string;
  role: "customer" | "assistant";
  runId: string | null;
  text: string;
};

export type PartialReply = { messageId: string; attempt: number; text: string };

export type ConversationState = {
  language: import("./i18n").TurnLanguage;
  lastSequence: number;
  messages: ChatMessage[];
  runs: Record<string, RunView>;
  runOrder: string[];
  activities: Record<string, Activity>;
  partial: Record<string, PartialReply>; // by run ID
  cases: Record<string, CaseLink>; // by case number, confirmed only
  staffReplies: Record<string, StaffReply>;
  staffNotices: ServerEvent[];
  gap: boolean;
};

export type StaffReply = {
  reply_id: string;
  case_number: string;
  text: string;
  posted_at: string;
  revision: number;
  after_message_id: string | null;
  translation: { status: "none" | "pending" | "done" | "failed"; text?: string;
                 reply_language?: string; reply_script?: string; source_language?: string };
};
