/**
 * Runs NabdRiskEngine outside the transaction that changed a case (Phase 6),
 * so a case command never waits for, or fails because of, risk evaluation.
 *
 * - After case changes: the Case trigger publishes one NABD_Risk_Check__e per
 *   affected customer (after commit). Its trigger evaluates them as the
 *   Automated Process user. Asynchronous Apex runs as whoever started it, and
 *   the restricted integration user (the case gateway's caller) cannot create
 *   warnings or tasks; a published event hands the work to the platform's own
 *   user instead. Locked rows are retried by the event bus.
 * - Saving a rule, and every hour (NabdRiskSchedule), queues a sweep of every
 *   relevant customer in chunks of CHUNK per chained job. The last chunk stamps
 *   the rules' Last_Evaluated_At__c, which the dashboards show as freshness.
 *
 * If a check cannot be published or queued (platform limits), the next hourly
 * sweep evaluates the customer anyway.
 */
public without sharing class NabdRiskEvaluation implements Queueable {
    public static final Integer CHUNK = 50;

    final Set<Id> accountIds;
    final Boolean isSweep;
    final Integer phase;
    final Id after;
    final Datetime startedAt;

    public NabdRiskEvaluation(Set<Id> accountIds) {
        this(accountIds, false, 0, null, null);
    }

    NabdRiskEvaluation(Set<Id> accountIds, Boolean isSweep, Integer phase, Id after, Datetime startedAt) {
        this.accountIds = accountIds;
        this.isSweep = isSweep;
        this.phase = phase;
        this.after = after;
        this.startedAt = startedAt;
    }

    public static NabdRiskEvaluation sweep() {
        return new NabdRiskEvaluation(new Set<Id>(), true, 0, null, null);
    }

    public void execute(QueueableContext context) {
        Datetime now = System.now();
        if (!isSweep) {
            List<Id> ordered = new List<Id>(accountIds);
            ordered.sort();
            Set<Id> batch = new Set<Id>();
            Set<Id> rest = new Set<Id>();
            for (Id accountId : ordered) {
                if (batch.size() < CHUNK) {
                    batch.add(accountId);
                } else {
                    rest.add(accountId);
                }
            }
            NabdRiskEngine.evaluate(batch, now);
            if (!rest.isEmpty()) {
                chain(new NabdRiskEvaluation(rest));
            }
            return;
        }
        Datetime started = startedAt == null ? now : startedAt;
        List<Id> page = NabdRiskEngine.sweepPage(phase, after, CHUNK + 1, now);
        Set<Id> batch = new Set<Id>();
        for (Integer i = 0; i < Math.min(CHUNK, page.size()); i++) {
            batch.add(page[i]);
        }
        NabdRiskEngine.evaluate(batch, now);
        if (page.size() > CHUNK) {
            chain(new NabdRiskEvaluation(new Set<Id>(), true, phase, page[CHUNK - 1], started));
        } else if (phase == 0) {
            chain(new NabdRiskEvaluation(new Set<Id>(), true, 1, null, started));
        } else {
            NabdRiskEngine.finishSweep(started);
        }
    }

    /** Tests cannot chain queueable jobs, so a test runs each chained step itself. */
    @TestVisible
    static List<NabdRiskEvaluation> chainedInTest = new List<NabdRiskEvaluation>();

    static void chain(NabdRiskEvaluation next) {
        if (Test.isRunningTest()) {
            chainedInTest.add(next);
        } else {
            System.enqueueJob(next);
        }
    }

    // --- Queueing ------------------------------------------------------------------------

    public static Boolean enqueueSweep() {
        if (!canQueue()) {
            return false;
        }
        System.enqueueJob(sweep());
        return true;
    }

    static Boolean canQueue() {
        return Limits.getQueueableJobs() < Limits.getLimitQueueableJobs();
    }

    // --- Triggers ------------------------------------------------------------------------

    static final List<String> CASE_FIELDS = new List<String>{
        'AccountId', 'Status', 'IsClosed', 'Priority', 'Negative_Streak__c', 'Resolution_Outcome__c',
        'Last_Reopened_At__c', 'NABD_Issue_Id__c'
    };

    /** After insert, update, delete or undelete of cases (previous is null except for updates). */
    public static void onCaseChange(List<Case> records, Map<Id, Case> previous) {
        publishChecks(changedCustomers(records, previous));
    }

    static Set<Id> changedCustomers(List<Case> records, Map<Id, Case> previous) {
        Set<Id> accounts = new Set<Id>();
        for (Case item : records) {
            Case before = previous == null ? null : previous.get(item.Id);
            Boolean nabd = String.isNotBlank(item.NABD_Issue_Id__c)
                || (before != null && String.isNotBlank(before.NABD_Issue_Id__c));
            if (!nabd || (before != null && !changed(item, before, CASE_FIELDS))) {
                continue;
            }
            accounts.add(item.AccountId);
            if (before != null) {
                accounts.add(before.AccountId); // a case moved to another customer changes both
            }
        }
        accounts.remove(null);
        return accounts;
    }

    /** One risk check per customer, delivered after the case change commits. */
    public static void publishChecks(Set<Id> accountIds) {
        List<NABD_Risk_Check__e> checks = new List<NABD_Risk_Check__e>();
        for (Id accountId : accountIds) {
            if (accountId != null) {
                checks.add(new NABD_Risk_Check__e(Account_Id__c = accountId));
            }
        }
        if (checks.isEmpty()) {
            return;
        }
        // Publishing needs Create on the event even from Apex; a refusal is not an exception. Log it:
        // the hourly sweep still evaluates the customer.
        for (Database.SaveResult result : EventBus.publish(checks)) {
            if (!result.isSuccess()) {
                System.debug(LoggingLevel.ERROR, 'NABD risk check not published: ' + result.getErrors());
            }
        }
    }

    @TestVisible
    static Integer maxRetries = 5;

    /**
     * The NABD_Risk_Check__e trigger: evaluates up to CHUNK customers per run. Later events
     * stay after the resume checkpoint and are delivered again; a locked customer is retried.
     */
    public static void onRiskChecks(List<NABD_Risk_Check__e> events) {
        Set<Id> ids = new Set<Id>();
        EventBus.TriggerContext context = EventBus.TriggerContext.currentContext();
        for (NABD_Risk_Check__e event : events) {
            Id accountId = toId(event.Account_Id__c);
            if (accountId != null && !ids.contains(accountId) && ids.size() == CHUNK) {
                break;
            }
            if (accountId != null) {
                ids.add(accountId);
            }
            context.setResumeCheckpoint(event.ReplayId);
        }
        try {
            NabdRiskEngine.evaluate(ids, System.now());
        } catch (Exception error) {
            Boolean locked = error.getMessage().contains('UNABLE_TO_LOCK_ROW') || error instanceof QueryException;
            if (locked && context.retries < maxRetries) {
                throw new EventBus.RetryableException('Customer records are locked; retrying risk evaluation.');
            }
            throw error;
        }
    }

    static Id toId(String value) {
        try {
            return String.isBlank(value) ? null : Id.valueOf(value);
        } catch (Exception error) {
            return null;
        }
    }

    static final List<String> RULE_FIELDS = new List<String>{
        'Active__c', 'Parameters_JSON__c', 'Version__c', 'Rule_Code__c', 'OwnerId'
    };

    @TestVisible
    static Boolean checkRules = true;

    /** Before insert/update: derive the rule key and refuse rules the engine cannot use. */
    public static void prepareRules(List<Risk_Rule__c> records) {
        for (Risk_Rule__c record : records) {
            String problem = checkRules ? NabdRiskEngine.problemWith(record) : null;
            if (problem != null) {
                record.addError(problem);
                continue;
            }
            if (record.Version__c != null) {
                record.Rule_Key__c = record.Rule_Code__c + '@' + record.Version__c.intValue();
            }
        }
    }

    /** After a rule's definition changes, sweep so warnings follow the new definition. */
    public static void onRuleChange(List<Risk_Rule__c> records, Map<Id, Risk_Rule__c> previous) {
        for (Risk_Rule__c record : records) {
            Risk_Rule__c before = previous == null ? null : previous.get(record.Id);
            if (before == null || changed(record, before, RULE_FIELDS)) {
                enqueueSweep();
                return;
            }
        }
    }

    static Boolean changed(SObject current, SObject before, List<String> fields) {
        for (String field : fields) {
            Object value = current.get(field);
            Object earlier = before.get(field);
            if (value == null ? earlier != null : !value.equals(earlier)) {
                return true;
            }
        }
        return false;
    }
}
