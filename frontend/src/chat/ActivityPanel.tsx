"use client";

import type { Activity, RunView } from "./types";
import { outcomeText, activityDetail, activityTitle, caseLine, runHeadline, seconds } from "./messages";

import { direction, localeOf, t, type Locale } from "./i18n";

function elapsed(a: { startedAt?: string; endedAt?: string; durationMs?: number }, now: number): number | null {
  if (a.durationMs != null) return a.durationMs;
  if (!a.startedAt) return null;
  const end = a.endedAt ? Date.parse(a.endedAt) : now;
  return Math.max(0, end - Date.parse(a.startedAt));
}

function StateIcon({ state, locale }: { state: string; locale: Locale }) {
  const label = t(locale, state === "queued" ? "queued" : state === "running" ? "running" : state === "waiting"
    ? "waiting" : state === "retrying" ? "retrying" : state === "completed" ? "completed" : "failed");
  return <span className={`state-icon state-${state}`} role="img" aria-label={label} title={label} />;
}

function Findings({ result, locale }: { result: Record<string, any>; locale: Locale }) {
  const facts: string[] = result.facts || [];
  const questions: string[] = result.questions || [];
  return (
    <div className="findings">
      {result.outcome && <div className={`outcome outcome-${result.outcome}`}>{outcomeText(result.outcome, locale)}</div>}
      {facts.length > 0 && (
        <><span className="label">{t(locale, "englishFindings")}</span>
        <ul className="facts" aria-label={t(locale, "findings")} lang="en" dir="ltr">
          {facts.map((fact, i) => <li key={i}>{fact}</li>)}
        </ul></>
      )}
      {questions.length > 0 && (
        <div className="questions">
          <span className="label">{t(locale, "missing")}</span>
          <ul lang="en" dir="ltr">{questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

function ActivityNode({ activity, byParent, now, locale }: { activity: Activity; byParent: Map<string, Activity[]>;
                                                     now: number; locale: Locale }) {
  const kids = byParent.get(activity.id) || [];
  const time = elapsed(activity, now);
  const detail = activityDetail(activity, locale);
  const articles: { title: string; revision: string }[] = activity.result?.articles || [];
  return (
    <li className={`activity activity-${activity.state}`}>
      <div className="activity-line">
        <StateIcon state={activity.state} locale={locale} />
        <span className="activity-title">{activityTitle(activity, locale)}</span>
        {time != null && <span className="activity-time" dir="ltr">{seconds(time)}</span>}
      </div>
      {detail && <div className="activity-detail">{detail}</div>}
      {articles.length > 0 && (
        <ul className="sources" aria-label={t(locale, "sources")} lang="en" dir="ltr">
          {articles.map((a) => <li key={a.revision}>{a.title} <bdi className="revision">({a.revision})</bdi></li>)}
        </ul>
      )}
      {activity.code === "agent.orders" && activity.state === "completed" && activity.result && (
        <Findings result={activity.result} locale={locale} />
      )}
      {activity.code.startsWith("case.") && activity.state === "completed" && activity.result?.case_number && (
        <div className="case-confirmed">{caseLine(activity.result, locale)}</div>
      )}
      {kids.length > 0 && (
        <ul className="activity-children">
          {kids.map((child) => <ActivityNode key={child.id} activity={child} byParent={byParent} now={now} locale={locale} />)}
        </ul>
      )}
    </li>
  );
}

export default function ActivityPanel({ run, activities, now, expanded, onToggle }: {
  run: RunView; activities: Activity[]; now: number; expanded: boolean; onToggle: () => void;
}) {
  const locale = localeOf(run.language);
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
    ? runHeadline(run, locale)
    : run.status === "failed"
      ? runHeadline(run, locale)
      : t(locale, "records", { count: checks }) + (total != null ? " · " + t(locale, "duration", { time: seconds(total) }) : "")
        + (run.attempt > 1 ? " · " + t(locale, "recovered", { count: run.attempt - 1 }) : "");
  return (
    <section className={`activity-panel run-${run.status}`} dir={direction(locale)} lang={locale.startsWith("ary") ? "ary" : locale} aria-live={active ? "polite" : "off"}>
      <button type="button" className="activity-toggle" onClick={onToggle} aria-expanded={expanded}>
        <StateIcon state={run.status === "retrying" ? "retrying" : run.status === "completed" ? "completed"
          : run.status === "failed" ? "failed" : run.status === "queued" ? "queued" : "running"} locale={locale} />
        <span className="activity-summary">{summary}</span>
        {active && total != null && <span className="activity-time" dir="ltr">{seconds(total)}</span>}
        <span className="chevron" aria-hidden>{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && roots.length > 0 && (
        <ul className="activity-tree">
          {roots.map((a) => <ActivityNode key={a.id} activity={a} byParent={byParent} now={now} locale={locale} />)}
        </ul>
      )}
      {expanded && roots.length === 0 && run.status === "queued" && (
        <p className="activity-empty">{t(locale, "previous")}</p>
      )}
    </section>
  );
}
