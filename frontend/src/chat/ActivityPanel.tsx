"use client";

import type { Activity, RunView } from "./types";
import { OUTCOME, activityDetail, activityTitle, caseLine, runHeadline, seconds } from "./messages";

function elapsed(a: { startedAt?: string; endedAt?: string; durationMs?: number }, now: number): number | null {
  if (a.durationMs != null) return a.durationMs;
  if (!a.startedAt) return null;
  const end = a.endedAt ? Date.parse(a.endedAt) : now;
  return Math.max(0, end - Date.parse(a.startedAt));
}

function StateIcon({ state }: { state: string }) {
  const label = { queued: "Queued", running: "Running", waiting: "Waiting", retrying: "Retrying",
                  completed: "Done", failed: "Failed" }[state] || state;
  return <span className={`state-icon state-${state}`} role="img" aria-label={label} title={label} />;
}

function Findings({ result }: { result: Record<string, any> }) {
  const facts: string[] = result.facts || [];
  const questions: string[] = result.questions || [];
  return (
    <div className="findings">
      {result.outcome && <div className={`outcome outcome-${result.outcome}`}>{OUTCOME[result.outcome] || result.outcome}</div>}
      {facts.length > 0 && (
        <ul className="facts" aria-label="Confirmed findings">
          {facts.map((fact, i) => <li key={i}>{fact}</li>)}
        </ul>
      )}
      {questions.length > 0 && (
        <div className="questions">
          <span className="label">Missing information</span>
          <ul>{questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

function ActivityNode({ activity, byParent, now }: { activity: Activity; byParent: Map<string, Activity[]>;
                                                     now: number }) {
  const kids = byParent.get(activity.id) || [];
  const time = elapsed(activity, now);
  const detail = activityDetail(activity);
  const articles: { title: string; revision: string }[] = activity.result?.articles || [];
  return (
    <li className={`activity activity-${activity.state}`}>
      <div className="activity-line">
        <StateIcon state={activity.state} />
        <span className="activity-title">{activityTitle(activity)}</span>
        {time != null && <span className="activity-time">{seconds(time)}</span>}
      </div>
      {detail && <div className="activity-detail">{detail}</div>}
      {articles.length > 0 && (
        <ul className="sources" aria-label="Sources">
          {articles.map((a) => <li key={a.revision}>{a.title} <span className="revision">({a.revision})</span></li>)}
        </ul>
      )}
      {activity.code === "agent.orders" && activity.state === "completed" && activity.result && (
        <Findings result={activity.result} />
      )}
      {activity.code.startsWith("case.") && activity.state === "completed" && activity.result?.case_number && (
        <div className="case-confirmed">{caseLine(activity.result)}</div>
      )}
      {kids.length > 0 && (
        <ul className="activity-children">
          {kids.map((child) => <ActivityNode key={child.id} activity={child} byParent={byParent} now={now} />)}
        </ul>
      )}
    </li>
  );
}

export default function ActivityPanel({ run, activities, now, expanded, onToggle }: {
  run: RunView; activities: Activity[]; now: number; expanded: boolean; onToggle: () => void;
}) {
  const ids = new Set(activities.map((a) => a.id));
  const byParent = new Map<string, Activity[]>();
  const roots: Activity[] = [];
  for (const a of [...activities].sort((x, y) => x.order - y.order)) {
    if (a.parentId && ids.has(a.parentId)) {
      byParent.set(a.parentId, [...(byParent.get(a.parentId) || []), a]);
    } else {
      roots.push(a);
    }
  }
  const active = run.status === "queued" || run.status === "running" || run.status === "retrying";
  const checks = activities.filter((a) => a.code.startsWith("tool.") && a.state === "completed").length;
  const total = elapsed({ startedAt: run.startedAt, endedAt: run.endedAt }, now);
  const summary = active
    ? runHeadline(run)
    : run.status === "failed"
      ? runHeadline(run)
      : `Checked ${checks} ${checks === 1 ? "record" : "records"}${total != null ? ` in ${seconds(total)}` : ""}`
        + (run.attempt > 1 ? ` · recovered after ${run.attempt - 1} ${run.attempt === 2 ? "retry" : "retries"}` : "");
  return (
    <section className={`activity-panel run-${run.status}`} aria-live={active ? "polite" : "off"}>
      <button type="button" className="activity-toggle" onClick={onToggle} aria-expanded={expanded}>
        <StateIcon state={run.status === "retrying" ? "retrying" : run.status === "completed" ? "completed"
          : run.status === "failed" ? "failed" : run.status === "queued" ? "queued" : "running"} />
        <span className="activity-summary">{summary}</span>
        {active && total != null && <span className="activity-time">{seconds(total)}</span>}
        <span className="chevron" aria-hidden>{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && roots.length > 0 && (
        <ul className="activity-tree">
          {roots.map((a) => <ActivityNode key={a.id} activity={a} byParent={byParent} now={now} />)}
        </ul>
      )}
      {expanded && roots.length === 0 && run.status === "queued" && (
        <p className="activity-empty">Waiting for the previous reply to finish.</p>
      )}
    </section>
  );
}
