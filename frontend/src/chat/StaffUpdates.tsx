import { direction, localeOf, ref, statusText, t, type Locale } from "./i18n";
import type { ServerEvent, StaffReply } from "./types";

export default function StaffUpdates({ replies, notices, locale }: {
  replies: StaffReply[]; notices: ServerEvent[]; locale: Locale;
}) {
  return <div className="staff-updates" aria-live="polite">
    {notices.map((event) => <p key={event.id} className="staff-notice" role="status">
      {event.type === "case.removed" ? t(locale, "caseRemoved", { number: ref(event.payload.case_number) })
        : t(locale, "staffUpdate", { number: ref(event.payload.case_number), status: statusText(event.payload.status, locale) })}
      {event.payload.changes?.includes("staff_action_completed") && <> {t(locale, "staffActionDone")}</>}
      {event.payload.changes?.includes("staff_action_changed") && <> {t(locale, "staffActionChanged")}</>}
    </p>)}
    {replies.map((reply) => {
      const tr = reply.translation;
      const translatedLocale = localeOf({ input_language: tr.source_language || "en",
        reply_language: tr.reply_language || "en", reply_script: tr.reply_script || "latin" });
      return <article key={reply.reply_id} className="bubble staff-reply">
        <strong>{t(locale, "staffReply", { number: ref(reply.case_number) })}</strong>
        <p className="muted staff-label">{t(locale, "staffOriginal")}</p>
        <p dir="auto">{reply.text}</p>
        {tr.status === "done" && <div className="staff-translation" lang={tr.reply_language} dir={direction(translatedLocale)}>
          <p className="muted staff-label">{t(locale, "staffTranslation")}</p><p>{tr.text}</p>
        </div>}
        {tr.status === "pending" && <p className="muted staff-label">{t(locale, "translationPending")}</p>}
        {tr.status === "failed" && <p className="muted staff-label">{t(locale, "translationFailed")}</p>}
      </article>;
    })}
  </div>;
}
