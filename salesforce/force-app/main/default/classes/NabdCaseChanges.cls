/**
 * Publishes NABD_Case_Change__e when staff change a customer-visible part of a
 * NABD case, or a public comment on one, so the customer's open chat can show it.
 *
 * The event is a platform event with "publish after commit": a rolled-back
 * staff edit never announces anything. It carries identifiers only (case,
 * comment, kind of change); the backend re-reads the case's current state as
 * the integration user, so the event can never leak comment text or internal
 * fields, and a duplicate or late event is harmless.
 *
 * NABD's own writes run through NabdCaseCommands, which sets `suppressed` for
 * its transaction: they do not echo back as staff changes.
 *
 * Private comments (IsPublished = false) announce nothing unless they were
 * public before, because withdrawing a reply is a change the chat must apply.
 */
public without sharing class NabdCaseChanges {
    public static Boolean suppressed = false;

    @TestVisible
    static List<NABD_Case_Change__e> published = new List<NABD_Case_Change__e>();

    // Case fields whose change the customer can see (status, staff action progress, resolution)
    // or that decide who may see the case at all (its account).
    static final List<String> CUSTOMER_VISIBLE = new List<String>{
        'Status', 'Staff_Action_Required__c', 'Staff_Action_Completed__c', 'Resolution_Outcome__c', 'AccountId'
    };

    public static void cases(List<Case> rows, Map<Id, Case> before, String change) {
        if (suppressed) {
            return;
        }
        List<NABD_Case_Change__e> events = new List<NABD_Case_Change__e>();
        for (Case row : rows) {
            if (String.isBlank(row.NABD_Issue_Id__c)) {
                continue; // not a NABD case: no chat can show it
            }
            if (before != null && !visibleChange(row, before.get(row.Id))) {
                continue;
            }
            events.add(new NABD_Case_Change__e(Case_Id__c = row.Id, Change__c = change));
        }
        publish(events);
    }

    public static void comments(List<CaseComment> rows, Map<Id, CaseComment> before, String change) {
        if (suppressed) {
            return;
        }
        Set<Id> parents = new Set<Id>();
        for (CaseComment row : rows) {
            parents.add(row.ParentId);
        }
        Set<Id> nabdCases = new Map<Id, Case>([
            SELECT Id FROM Case WHERE Id IN :parents AND NABD_Issue_Id__c != null WITH SYSTEM_MODE
        ]).keySet();
        List<NABD_Case_Change__e> events = new List<NABD_Case_Change__e>();
        for (CaseComment row : rows) {
            if (!nabdCases.contains(row.ParentId)) {
                continue;
            }
            CaseComment old = before == null ? null : before.get(row.Id);
            Boolean wasPublic = old != null && old.IsPublished;
            if (!row.IsPublished && !wasPublic) {
                continue; // a private note stays private: nothing for the customer changed
            }
            events.add(new NABD_Case_Change__e(Case_Id__c = row.ParentId, Comment_Id__c = row.Id, Change__c = change));
        }
        publish(events);
    }

    static Boolean visibleChange(Case row, Case old) {
        if (old == null) {
            return true;
        }
        for (String field : CUSTOMER_VISIBLE) {
            if (row.get(field) != old.get(field)) {
                return true;
            }
        }
        return false;
    }

    static void publish(List<NABD_Case_Change__e> events) {
        if (events.isEmpty()) {
            return;
        }
        published.addAll(events);
        // A failed publish must never block staff work; NABD's periodic reconciliation catches up.
        for (Database.SaveResult result : EventBus.publish(events)) {
            if (!result.isSuccess()) {
                System.debug(LoggingLevel.WARN, 'NABD case change event not published: ' + result.getErrors());
            }
        }
    }
}
