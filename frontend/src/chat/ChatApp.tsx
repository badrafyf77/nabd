"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ActivityPanel from "./ActivityPanel";
import { ApiError, EVENT_TYPES, api } from "./api";
import { CASE_STATUS } from "./messages";
import { applyEvents, emptyState, fromSnapshot } from "./reducer";
import type { ConversationState, ConversationSummary, DemoCustomer, ServerEvent, Session } from "./types";
import "./chat.css";

type Pending = { clientId: string; text: string; failed?: boolean };
type Connection = "connecting" | "live" | "reconnecting";

const STALE_STREAM_MS = 15_000; // three missed 5 s heartbeats

const LANGUAGES: Record<string, string> = { en: "English", fr: "French", ar: "Arabic", ary: "Darija" };

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function SignIn({ onSignedIn }: { onSignedIn: (session: Session) => void }) {
  const [customers, setCustomers] = useState<DemoCustomer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    api.customers().then((r) => setCustomers(r.customers)).catch((e: ApiError) => setError(e.code));
  }, []);

  async function choose(customerId: string) {
    setBusy(customerId);
    setError(null);
    try {
      onSignedIn(await api.signIn(customerId));
    } catch (e) {
      setError((e as ApiError).code);
      setBusy(null);
    }
  }

  return (
    <main className="signin">
      <p className="eyebrow">ShopEasy support · demo</p>
      <h1>Choose a demo customer</h1>
      <p className="signin-note">
        This demo signs you in as one of ShopEasy&apos;s seeded customers. The assistant only sees that
        customer&apos;s orders, cases and the published help articles in Salesforce.
      </p>
      {error && <p className="error" role="alert">Sign-in is unavailable right now ({error}).</p>}
      {!customers && !error && <p className="muted">Loading customers from Salesforce…</p>}
      <ul className="customer-grid">
        {customers?.map((c) => (
          <li key={c.customer_id}>
            <button type="button" onClick={() => choose(c.customer_id)} disabled={busy !== null}>
              <span className="customer-name">{c.first_name} {c.last_name}</span>
              <span className="customer-meta">
                {c.customer_id}{c.preferred_language ? ` · ${LANGUAGES[c.preferred_language] || c.preferred_language}` : ""}
              </span>
              {busy === c.customer_id && <span className="muted">Signing in…</span>}
            </button>
          </li>
        ))}
      </ul>
    </main>
  );
}

function Chat({ session, onSignedOut }: { session: Session; onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [state, setState] = useState<ConversationState>(emptyState());
  const [loading, setLoading] = useState(false);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [reloadKey, setReloadKey] = useState(0);
  const [pending, setPending] = useState<Pending[]>([]);
  const [draft, setDraft] = useState("");
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const [now, setNow] = useState(() => Date.now());
  const [notice, setNotice] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const lastSequence = useRef(0);

  const handleAuthError = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) onSignedOut();
    else setNotice(error instanceof ApiError ? error.code : "network_error");
  }, [onSignedOut]);

  const selectConversation = useCallback((id: string) => {
    setCurrentId(id);
    const url = new URL(window.location.href);
    url.searchParams.set("c", id);
    window.history.replaceState(null, "", url);
  }, []);

  // Conversation list; restore the one in the URL, else the latest, else start one.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { conversations: list } = await api.conversations();
        if (cancelled) return;
        setConversations(list);
        const wanted = new URL(window.location.href).searchParams.get("c");
        const chosen = list.find((c) => c.id === wanted) ?? list[0];
        if (chosen) selectConversation(chosen.id);
        else {
          const created = await api.createConversation();
          if (cancelled) return;
          setConversations([created]);
          selectConversation(created.id);
        }
      } catch (error) {
        handleAuthError(error);
      }
    })();
    return () => { cancelled = true; };
  }, [handleAuthError, selectConversation]);

  // Snapshot, then live events after it. EventSource resends Last-Event-ID when it
  // reconnects by itself; a missing heartbeat (a half-open proxied stream) reopens it.
  useEffect(() => {
    if (!currentId) return;
    let source: EventSource | null = null;
    let watchdog: number | undefined;
    let cancelled = false;
    let lastSeen = Date.now();
    setLoading(true);
    setConnection("connecting");

    const onEvent = (message: MessageEvent) => {
      lastSeen = Date.now();
      const event: ServerEvent = JSON.parse(message.data);
      lastSequence.current = Math.max(lastSequence.current, event.sequence);
      setState((s) => applyEvents(s, [event]));
      if (event.type === "message.created") {
        setPending((p) => p.filter((item) => item.clientId !== event.payload.client_message_id));
      }
      if (event.type === "message.created" || event.type === "run.completed") {
        api.conversations().then((r) => setConversations(r.conversations)).catch(() => {});
      }
    };
    const open = () => {
      source?.close();
      source = new EventSource(`/api/conversations/${currentId}/events?after=${lastSequence.current}`);
      source.onopen = () => {
        lastSeen = Date.now();
        setConnection("live");
      };
      const current = source;
      source.onerror = () => {
        setConnection("reconnecting");
        // An HTTP error (backend down behind the proxy) closes EventSource for good: reopen it.
        if (current.readyState === EventSource.CLOSED && !cancelled) {
          window.setTimeout(() => { if (!cancelled && source === current) open(); }, 2_000);
        }
      };
      for (const type of EVENT_TYPES) source.addEventListener(type, onEvent as EventListener);
      source.addEventListener("ping", () => {
        lastSeen = Date.now();
        setConnection("live");
      });
      source.addEventListener("session.ended", () => onSignedOut());
    };

    api.snapshot(currentId).then((snapshot) => {
      if (cancelled) return;
      setState(fromSnapshot(snapshot));
      lastSequence.current = snapshot.last_sequence;
      setLoading(false);
      open();
      watchdog = window.setInterval(() => {
        if (Date.now() - lastSeen > STALE_STREAM_MS) {
          setConnection("reconnecting");
          lastSeen = Date.now();
          open();
        }
      }, 5_000);
    }).catch((error) => {
      if (!cancelled) {
        setLoading(false);
        handleAuthError(error);
      }
    });
    return () => {
      cancelled = true;
      window.clearInterval(watchdog);
      source?.close();
    };
  }, [currentId, reloadKey, handleAuthError, onSignedOut]);

  // A sequence gap means events were missed: reload the snapshot rather than guess.
  useEffect(() => {
    if (state.gap) setReloadKey((k) => k + 1);
  }, [state.gap]);

  const active = useMemo(
    () => Object.values(state.runs).some((r) => r.status === "queued" || r.status === "running" || r.status === "retrying"),
    [state.runs],
  );
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [state.messages.length, state.partial, pending.length, state.runOrder.length]);

  async function send(text: string, clientId = newId()) {
    if (!currentId || !text.trim()) return;
    setPending((p) => [...p.filter((x) => x.clientId !== clientId), { clientId, text }]);
    try {
      await api.send(currentId, clientId, text);
    } catch (error) {
      setPending((p) => p.map((x) => (x.clientId === clientId ? { ...x, failed: true } : x)));
      handleAuthError(error);
    }
  }

  async function newConversation() {
    try {
      const created = await api.createConversation();
      setConversations((list) => [created, ...list]);
      setState(emptyState());
      selectConversation(created.id);
    } catch (error) {
      handleAuthError(error);
    }
  }

  async function signOut() {
    try {
      await api.signOut();
    } finally {
      window.history.replaceState(null, "", window.location.pathname);
      onSignedOut();
    }
  }

  const messagesByRun = useMemo(() => {
    const map = new Map<string, { customer?: string; assistant?: string }>();
    for (const m of state.messages) {
      if (!m.runId) continue;
      const item = map.get(m.runId) || {};
      item[m.role] = m.text;
      map.set(m.runId, item);
    }
    return map;
  }, [state.messages]);

  const activitiesByRun = useMemo(() => {
    const map = new Map<string, typeof state.activities[string][]>();
    for (const a of Object.values(state.activities)) map.set(a.runId, [...(map.get(a.runId) || []), a]);
    return map;
  }, [state.activities]);

  return (
    <div className="chat-shell">
      <aside className="sidebar">
        <div className="brand">
          <p className="eyebrow">ShopEasy</p>
          <h1>Support</h1>
        </div>
        <button type="button" className="new-chat" onClick={newConversation}>New conversation</button>
        <nav aria-label="Conversations">
          <ul className="conversation-list">
            {conversations.map((c) => (
              <li key={c.id}>
                <button type="button" className={c.id === currentId ? "current" : ""}
                        onClick={() => selectConversation(c.id)}>
                  {c.title || "New conversation"}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="who">
          <span>Signed in as <strong>{session.display_name}</strong></span>
          <span className="muted">{session.customer_id} · demo account</span>
          <button type="button" className="link" onClick={signOut}>Sign out</button>
        </div>
      </aside>

      <main className="chat-main">
        <header className="chat-header">
          <div>
            <h2>Hi {session.first_name}, how can we help?</h2>
            <p className="muted">Ask about your orders, deliveries, returns or ShopEasy&apos;s policies.</p>
          </div>
          <span className={`connection connection-${connection}`} role="status">
            {connection === "live" ? "Live" : connection === "connecting" ? "Connecting…" : "Reconnecting…"}
          </span>
        </header>
        {Object.keys(state.cases).length > 0 && (
          <ul className="case-strip" aria-label="Support cases in this conversation">
            {Object.values(state.cases).map((c) => (
              <li key={c.case_number}>
                <strong>Case {c.case_number}</strong>
                <span>{c.department ? `${c.department} team` : "a team member"}</span>
                {c.status && <span className="muted">{CASE_STATUS[c.status] || c.status}</span>}
              </li>
            ))}
          </ul>
        )}
        {notice && (
          <p className="error banner" role="alert">
            Something went wrong ({notice}). <button type="button" className="link" onClick={() => setNotice(null)}>Dismiss</button>
          </p>
        )}

        <div className="timeline" aria-busy={loading}>
          {loading && <p className="muted">Loading conversation…</p>}
          {!loading && state.runOrder.length === 0 && pending.length === 0 && (
            <p className="empty">Try “Where is my order?” or “How much does delivery cost?”</p>
          )}
          {state.runOrder.map((runId) => {
            const run = state.runs[runId];
            const texts = messagesByRun.get(runId) || {};
            const partial = state.partial[runId];
            const finished = run.status === "completed" || run.status === "failed";
            const expanded = toggled[runId] ?? !finished;
            return (
              <div key={runId} className="turn">
                {texts.customer && <div className="bubble customer" dir="auto">{texts.customer}</div>}
                <ActivityPanel run={run} activities={activitiesByRun.get(runId) || []} now={now} expanded={expanded}
                               onToggle={() => setToggled((t) => ({ ...t, [runId]: !expanded }))} />
                {texts.assistant && <div className="bubble assistant" dir="auto">{texts.assistant}</div>}
                {!texts.assistant && partial && (
                  <div className="bubble assistant streaming" dir="auto">{partial.text}<span className="caret" /></div>
                )}
                {run.status === "failed" && texts.customer && (
                  <button type="button" className="link retry" onClick={() => send(texts.customer!)}>
                    Send this message again
                  </button>
                )}
              </div>
            );
          })}
          {pending.map((p) => (
            <div key={p.clientId} className="turn">
              <div className="bubble customer pending" dir="auto">{p.text}</div>
              <p className={p.failed ? "error small" : "muted small"}>
                {p.failed ? <>Not sent. <button type="button" className="link" onClick={() => send(p.text, p.clientId)}>Retry</button></> : "Sending…"}
              </p>
            </div>
          ))}
          <div ref={bottom} />
        </div>

        <form className="composer" onSubmit={(e) => {
          e.preventDefault();
          const text = draft.trim();
          if (!text) return;
          setDraft("");
          send(text);
        }}>
          <label htmlFor="message" className="visually-hidden">Message</label>
          <textarea id="message" value={draft} rows={2} maxLength={4000} dir="auto"
                    placeholder={active ? "You can keep typing; your next message will queue." : "Write a message…"}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault();
                        e.currentTarget.form?.requestSubmit();
                      }
                    }} />
          <button type="submit" disabled={!draft.trim() || !currentId}>Send</button>
        </form>
      </main>
    </div>
  );
}

export default function ChatApp() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  useEffect(() => {
    api.session().then(setSession).catch(() => setSession(null));
  }, []);

  if (session === undefined) return <main className="signin"><p className="muted">Loading…</p></main>;
  if (session === null) return <SignIn onSignedIn={setSession} />;
  return <Chat key={session.customer_id} session={session} onSignedOut={() => setSession(null)} />;
}
