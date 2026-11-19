/**
 * NABD's explainable early-warning engine (Phase 6).
 *
 * Rules are Risk_Rule__c records; the highest active version of each known
 * code is used. For one customer a rule either holds, with the cases and
 * evidence that support it, or it does not. The engine keeps one warning
 * (Risk_Episode__c) per customer and rule code while the rule holds:
 *
 *   holds, no active warning -> open one, with its evidence and one follow-up Task
 *   holds, active warning    -> update version, explanation and evidence; never a second task
 *   no longer holds          -> clear it, keep its history, complete an open task with a note
 *
 * Active_Key__c (customer:code, unique, emptied on clear) makes Salesforce
 * refuse a second active warning, and evaluation locks the customers' account
 * rows first, as the case command gateway does. A complaint is a NABD case
 * (NABD_Issue_Id__c): one distinct issue, never a message. A contact is one
 * gateway command with new customer evidence (Negative_Streak__c).
 *
 * Explanations are staff-facing English and quote no customer text. Risk
 * records, tasks and sentiment are not readable by the integration user, so
 * nothing here can reach a customer's chat. Every query states WITH
 * SYSTEM_MODE because evaluation may run as the integration user.
 */
public without sharing class NabdRiskEngine {
    public static final String REPEAT = 'repeat_complaints';
    public static final String HIGH_UNRESOLVED = 'high_priority_unresolved';
    public static final String NEGATIVE = 'persistent_negative_sentiment';
    public static final List<String> CODES = new List<String>{REPEAT, HIGH_UNRESOLVED, NEGATIVE};
    static final Set<String> NEGATIVE_SENTIMENTS = new Set<String>{'negative', 'very_negative'};
    static final Set<String> CONTACT_KINDS = new Set<String>{'customer_report', 'customer_confirmation'};
    static final Set<String> PRIORITIES = new Set<String>{'High', 'Medium', 'Low'};
    static final Integer MAX_EXPLANATION = 4000;
    static final Integer MAX_LINES = 25;

    public class RuleException extends Exception {}

    /** A usable rule version with validated parameters. */
    public class Rule {
        public Risk_Rule__c record;
        public String code;
        public Integer version;
        public Integer threshold;  // repeat_complaints: distinct complaints
        public Integer windowDays; // repeat_complaints
        public String priority;    // high_priority_unresolved
        public Integer hours;      // high_priority_unresolved
        public Integer followUps;  // persistent_negative_sentiment
    }

    public class Rules {
        public Map<String, Rule> usable = new Map<String, Rule>();
        public Set<String> misconfigured = new Set<String>();
        public Map<Id, String> errors = new Map<Id, String>();
        public Integer maxWindowDays = 1;
    }

    public class Support {
        public String key;
        public Id caseId;
        public Id evidenceId;
        public String detail;
        public Datetime observedAt;

        Support(String key, Id caseId, Id evidenceId, String detail, Datetime observedAt) {
            this.key = key;
            this.caseId = caseId;
            this.evidenceId = evidenceId;
            this.detail = detail.left(255);
            this.observedAt = observedAt;
        }
    }

    public class Finding {
        public Rule rule;
        public String summary;
        public List<Support> supports = new List<Support>();

        Finding(Rule rule) {
            this.rule = rule;
        }
    }

    public class Summary {
        public Integer customers = 0;
        public Integer opened = 0;
        public Integer updated = 0;
        public Integer cleared = 0;
        public Integer tasksCreated = 0;
        public Integer tasksCompleted = 0;
    }

    // --- Rules ---------------------------------------------------------------------------

    /** Null when the rule can be saved, otherwise a message for staff. */
    public static String problemWith(Risk_Rule__c record) {
        try {
            parse(record);
            return null;
        } catch (RuleException error) {
            return error.getMessage();
        }
    }

    public static Rule parse(Risk_Rule__c record) {
        Rule rule = new Rule();
        rule.record = record;
        rule.code = record.Rule_Code__c;
        rule.version = record.Version__c == null ? null : record.Version__c.intValue();
        if (rule.code == null || !new Set<String>(CODES).contains(rule.code)) {
            throw new RuleException('Rule Code must be one of: ' + String.join(CODES, ', ') + '.');
        }
        Map<String, Object> params;
        try {
            Object parsed = JSON.deserializeUntyped(String.isBlank(record.Parameters_JSON__c) ? '{}' : record.Parameters_JSON__c);
            params = (Map<String, Object>) parsed;
        } catch (Exception error) {
            throw new RuleException('Parameters (JSON) must be a JSON object.');
        }
        if (REPEAT.equals(rule.code)) {
            rule.threshold = whole(params, 'distinct_complaints', 2, 100);
            rule.windowDays = whole(params, 'window_days', 1, 365);
        } else if (HIGH_UNRESOLVED.equals(rule.code)) {
            Object value = params.get('priority');
            if (!(value instanceof String) || !PRIORITIES.contains((String) value)) {
                throw new RuleException('"priority" must be "High", "Medium" or "Low".');
            }
            rule.priority = (String) value;
            rule.hours = whole(params, 'unresolved_hours', 1, 8760);
        } else {
            rule.followUps = whole(params, 'follow_ups', 1, 20);
        }
        return rule;
    }

    static Integer whole(Map<String, Object> params, String name, Integer low, Integer high) {
        Object value = params.get(name);
        Long whole = null;
        if (value instanceof Integer) {
            whole = (Integer) value;
        } else if (value instanceof Long) {
            whole = (Long) value;
        }
        if (whole == null || whole < low || whole > high) {
            throw new RuleException('"' + name + '" must be a whole number from ' + low + ' to ' + high + '.');
        }
        return whole.intValue();
    }

    public static Rules loadRules() {
        Rules rules = new Rules();
        Map<String, Integer> newest = new Map<String, Integer>();
        for (Risk_Rule__c record : [
            SELECT Id, Name, Rule_Code__c, Version__c, Parameters_JSON__c, OwnerId
            FROM Risk_Rule__c WHERE Active__c = true WITH SYSTEM_MODE ORDER BY Rule_Code__c, Version__c DESC
        ]) {
            String code = record.Rule_Code__c;
            if (newest.containsKey(code)) {
                rules.errors.put(record.Id, ('Ignored: version ' + newest.get(code) + ' of this rule is active.').left(255));
                continue;
            }
            newest.put(code, record.Version__c == null ? 0 : record.Version__c.intValue());
            try {
                Rule rule = parse(record);
                rules.usable.put(code, rule);
                rules.errors.put(record.Id, null);
                if (rule.windowDays != null) {
                    rules.maxWindowDays = Math.max(rules.maxWindowDays, rule.windowDays);
                }
            } catch (RuleException error) {
                rules.misconfigured.add(code);
                rules.errors.put(record.Id, error.getMessage().left(255));
            }
        }
        return rules;
    }

    /** Records each version's last error, and the sweep time on the versions in use. */
    public static void saveRuleState(Rules rules, Datetime sweptAt) {
        Set<Id> used = new Set<Id>();
        for (Rule rule : rules.usable.values()) {
            used.add(rule.record.Id);
        }
        List<Risk_Rule__c> changes = new List<Risk_Rule__c>();
        for (Risk_Rule__c record : [
            SELECT Id, Active__c, Last_Error__c FROM Risk_Rule__c WITH SYSTEM_MODE
        ]) {
            String error = record.Active__c ? rules.errors.get(record.Id) : null;
            Risk_Rule__c change = new Risk_Rule__c(Id = record.Id);
            Boolean changed = false;
            if (!same(record.Last_Error__c, error)) {
                change.Last_Error__c = error;
                changed = true;
            }
            if (sweptAt != null && used.contains(record.Id)) {
                change.Last_Evaluated_At__c = sweptAt;
                changed = true;
            }
            if (changed) {
                changes.add(change);
            }
        }
        if (!changes.isEmpty()) {
            update as system changes;
        }
    }

    // --- Evaluation ----------------------------------------------------------------------

    public static Summary evaluate(Set<Id> accountIds, Datetime now) {
        Summary summary = new Summary();
        Set<Id> requested = new Set<Id>(accountIds);
        requested.remove(null);
        if (requested.isEmpty()) {
            return summary;
        }
        Rules rules = loadRules();
        saveRuleState(rules, null);
        // Lock the customers first: case commands and other evaluations of them wait (no ORDER BY with locks).
        List<Account> accounts = [
            SELECT Id, Name, ShopEasy_Customer_Id__c FROM Account WHERE Id IN :requested WITH SYSTEM_MODE FOR UPDATE
        ];
        Set<Id> ids = new Map<Id, Account>(accounts).keySet();
        summary.customers = accounts.size();
        if (accounts.isEmpty()) {
            return summary;
        }
        Datetime windowStart = now.addDays(-rules.maxWindowDays);

        Map<Id, List<Case>> casesByAccount = new Map<Id, List<Case>>();
        Map<Id, Case> streakCases = new Map<Id, Case>();
        Rule negative = rules.usable.get(NEGATIVE);
        for (Case item : [
            SELECT Id, CaseNumber, AccountId, Status, IsClosed, Priority, CreatedDate, Last_Reopened_At__c,
                   Resolution_Outcome__c, Negative_Streak__c, Complaint_Category__c, NABD_Open_Since__c
            FROM Case
            WHERE AccountId IN :ids AND NABD_Issue_Id__c != null
              AND (IsClosed = false OR CreatedDate >= :windowStart OR Last_Reopened_At__c >= :windowStart)
            WITH SYSTEM_MODE ORDER BY CreatedDate, CaseNumber
        ]) {
            if (!casesByAccount.containsKey(item.AccountId)) {
                casesByAccount.put(item.AccountId, new List<Case>());
            }
            casesByAccount.get(item.AccountId).add(item);
            if (negative != null && !item.IsClosed && streak(item) >= negative.followUps + 1) {
                streakCases.put(item.Id, item);
            }
        }
        Map<Id, List<Complaint_Evidence__c>> contactsByCase = new Map<Id, List<Complaint_Evidence__c>>();
        if (!streakCases.isEmpty()) {
            Set<Id> streakIds = streakCases.keySet();
            for (Complaint_Evidence__c row : [
                SELECT Id, Name, Case__c, Captured_At__c, CreatedDate, Assessed_Sentiment__c
                FROM Complaint_Evidence__c
                WHERE Case__c IN :streakIds AND Evidence_Kind__c IN :CONTACT_KINDS
                WITH SYSTEM_MODE ORDER BY Captured_At__c DESC NULLS LAST, CreatedDate DESC
            ]) {
                if (!contactsByCase.containsKey(row.Case__c)) {
                    contactsByCase.put(row.Case__c, new List<Complaint_Evidence__c>());
                }
                contactsByCase.get(row.Case__c).add(row);
            }
        }

        Map<String, Risk_Episode__c> active = new Map<String, Risk_Episode__c>();
        for (Risk_Episode__c episode : [
            SELECT Id, Account__c, Episode_Key__c, Active_Key__c, Rule_Code__c, Rule_Version__c, Risk_Rule__c,
                   Explanation__c, Evidence_Count__c, Follow_Up_Task_Id__c
            FROM Risk_Episode__c WHERE Account__c IN :ids AND Active_Key__c != null WITH SYSTEM_MODE
        ]) {
            active.put(episode.Active_Key__c, episode);
        }
        Map<Id, Map<String, Risk_Evidence__c>> rowsByEpisode = new Map<Id, Map<String, Risk_Evidence__c>>();
        Set<Id> taskIds = new Set<Id>();
        for (Risk_Episode__c episode : active.values()) {
            rowsByEpisode.put(episode.Id, new Map<String, Risk_Evidence__c>());
            Id taskId = toId(episode.Follow_Up_Task_Id__c);
            if (taskId != null) {
                taskIds.add(taskId);
            }
        }
        if (!rowsByEpisode.isEmpty()) {
            Set<Id> episodeIds = rowsByEpisode.keySet();
            for (Risk_Evidence__c row : [
                SELECT Id, Risk_Episode__c, Evidence_Key__c, Current__c, Detail__c
                FROM Risk_Evidence__c WHERE Risk_Episode__c IN :episodeIds WITH SYSTEM_MODE
            ]) {
                rowsByEpisode.get(row.Risk_Episode__c).put(row.Evidence_Key__c, row);
            }
        }
        Map<Id, Task> tasks = new Map<Id, Task>([
            SELECT Id, IsClosed, Description FROM Task WHERE Id IN :taskIds WITH SYSTEM_MODE
        ]);
        Map<Id, Id> contactByAccount = new Map<Id, Id>();
        for (Contact person : [
            SELECT Id, AccountId FROM Contact WHERE AccountId IN :ids WITH SYSTEM_MODE ORDER BY CreatedDate
        ]) {
            if (!contactByAccount.containsKey(person.AccountId)) {
                contactByAccount.put(person.AccountId, person.Id);
            }
        }

        Changes changes = new Changes(now);
        for (Account account : accounts) {
            List<Case> cases = casesByAccount.containsKey(account.Id) ? casesByAccount.get(account.Id) : new List<Case>();
            String customer = String.isBlank(account.ShopEasy_Customer_Id__c)
                ? String.valueOf(account.Id) : account.ShopEasy_Customer_Id__c;
            for (String code : CODES) {
                if (rules.misconfigured.contains(code)) {
                    continue; // a configuration error never clears warnings; Last_Error__c explains it
                }
                Rule rule = rules.usable.get(code);
                Finding finding = rule == null ? null : find(rule, cases, contactsByCase, now);
                String activeKey = (customer + ':' + code).left(100);
                Risk_Episode__c episode = active.get(activeKey);
                if (finding != null && episode == null) {
                    changes.open(account, customer, activeKey, finding, contactByAccount.get(account.Id));
                    summary.opened++;
                } else if (finding != null) {
                    changes.refresh(episode, finding, rowsByEpisode.get(episode.Id), tasks.get(toId(episode.Follow_Up_Task_Id__c)));
                    summary.updated++;
                } else if (episode != null) {
                    String reason = rule == null ? 'The rule is no longer active.' : clearReason(rule);
                    if (changes.clear(episode, reason, rowsByEpisode.get(episode.Id), tasks.get(toId(episode.Follow_Up_Task_Id__c)))) {
                        summary.tasksCompleted++;
                    }
                    summary.cleared++;
                }
            }
        }
        summary.tasksCreated = changes.commitAll();
        return summary;
    }

    static Finding find(Rule rule, List<Case> cases, Map<Id, List<Complaint_Evidence__c>> contacts, Datetime now) {
        if (REPEAT.equals(rule.code)) {
            return repeatComplaints(rule, cases, now);
        }
        if (HIGH_UNRESOLVED.equals(rule.code)) {
            return highPriorityUnresolved(rule, cases, now);
        }
        return negativeSentiment(rule, cases, contacts);
    }

    static Finding repeatComplaints(Rule rule, List<Case> cases, Datetime now) {
        Datetime start = now.addDays(-rule.windowDays);
        Finding finding = new Finding(rule);
        for (Case item : cases) {
            if ('duplicate'.equals(item.Resolution_Outcome__c)) {
                continue; // staff decided it is the same issue as another case
            }
            Boolean opened = item.CreatedDate >= start;
            Boolean reopened = item.Last_Reopened_At__c != null && item.Last_Reopened_At__c >= start;
            if (!opened && !reopened) {
                continue;
            }
            String detail = caseLine(item) + (reopened ? ' · reopened ' + stamp(item.Last_Reopened_At__c) : '');
            finding.supports.add(new Support(String.valueOf(item.Id), item.Id, null, detail,
                                             reopened ? item.Last_Reopened_At__c : item.CreatedDate));
        }
        if (finding.supports.size() < rule.threshold) {
            return null;
        }
        finding.summary = finding.supports.size() + ' distinct complaints were opened or reopened in the last '
            + rule.windowDays + ' days; this rule warns at ' + rule.threshold
            + '. Cases closed as duplicates are not counted.';
        return finding;
    }

    static Finding highPriorityUnresolved(Rule rule, List<Case> cases, Datetime now) {
        Datetime latest = now.addHours(-rule.hours);
        Finding finding = new Finding(rule);
        for (Case item : cases) {
            Datetime since = openSince(item);
            if (item.IsClosed || !rule.priority.equals(item.Priority) || since > latest) {
                continue;
            }
            Long hoursOpen = (now.getTime() - since.getTime()) / 3600000L;
            finding.supports.add(new Support(String.valueOf(item.Id), item.Id, null,
                caseLine(item) + ' · open since ' + stamp(since) + ' (' + hoursOpen + ' hours)', since));
        }
        if (finding.supports.isEmpty()) {
            return null;
        }
        finding.summary = finding.supports.size() + ' open ' + rule.priority + '-priority '
            + (finding.supports.size() == 1 ? 'complaint has' : 'complaints have')
            + ' been unresolved for at least ' + rule.hours + ' hours.';
        return finding;
    }

    static Finding negativeSentiment(Rule rule, List<Case> cases, Map<Id, List<Complaint_Evidence__c>> contacts) {
        Integer needed = rule.followUps + 1;
        Finding finding = new Finding(rule);
        for (Case item : cases) {
            if (item.IsClosed || streak(item) < needed) {
                continue;
            }
            finding.supports.add(new Support(String.valueOf(item.Id), item.Id, null,
                caseLine(item) + ' · negative at ' + streak(item) + ' consecutive contacts', openSince(item)));
            Integer cited = 0;
            List<Complaint_Evidence__c> rows = contacts.containsKey(item.Id) ? contacts.get(item.Id) : new List<Complaint_Evidence__c>();
            for (Complaint_Evidence__c row : rows) {
                if (row.Assessed_Sentiment__c == null) {
                    continue; // recorded before contacts carried sentiment
                }
                if (!NEGATIVE_SENTIMENTS.contains(row.Assessed_Sentiment__c) || cited == needed) {
                    break; // only the newest run of negative contacts supports the warning
                }
                Datetime seen = row.Captured_At__c == null ? row.CreatedDate : row.Captured_At__c;
                finding.supports.add(new Support(String.valueOf(item.Id) + ':' + String.valueOf(row.Id), item.Id, row.Id,
                    'Case ' + item.CaseNumber + ' · customer words ' + row.Name + ' · ' + row.Assessed_Sentiment__c
                    + ' · ' + stamp(seen), seen));
                cited++;
            }
        }
        if (finding.supports.isEmpty()) {
            return null;
        }
        finding.summary = 'NABD assessed the customer as negative at ' + needed
            + ' or more consecutive contacts about the same open complaint (a contact and '
            + (rule.followUps == 1 ? 'the next follow-up' : 'the next ' + rule.followUps + ' follow-ups') + ').';
        return finding;
    }

    static String clearReason(Rule rule) {
        if (REPEAT.equals(rule.code)) {
            return 'Fewer than ' + rule.threshold + ' distinct complaints were opened or reopened in the last '
                + rule.windowDays + ' days.';
        }
        if (HIGH_UNRESOLVED.equals(rule.code)) {
            return 'No open ' + rule.priority + '-priority complaint has been unresolved for ' + rule.hours + ' hours.';
        }
        return 'No open complaint is negative at ' + (rule.followUps + 1) + ' consecutive contacts any more.';
    }

    public static String explanation(Finding finding) {
        Rule rule = finding.rule;
        List<String> lines = new List<String>{
            rule.record.Name + ' (' + rule.code + ', version ' + rule.version + ')',
            finding.summary
        };
        Integer shown = 0;
        for (Support support : finding.supports) {
            if (shown == MAX_LINES) {
                lines.add('… and ' + (finding.supports.size() - shown) + ' more; see the Risk Evidence list.');
                break;
            }
            lines.add('• ' + support.detail);
            shown++;
        }
        return String.join(lines, '\n').left(MAX_EXPLANATION);
    }

    // --- Writes -------------------------------------------------------------------------

    /** Collects every write of one evaluation so it is sent in a few bulk statements. */
    class Changes {
        final Datetime now;
        final String openStatus;
        final String closedStatus;
        final List<Risk_Episode__c> newEpisodes = new List<Risk_Episode__c>();
        final List<Finding> newFindings = new List<Finding>();
        final List<Id> newContacts = new List<Id>();
        final List<String> newSubjects = new List<String>();
        final List<Risk_Episode__c> updates = new List<Risk_Episode__c>();
        final List<Risk_Evidence__c> evidenceInserts = new List<Risk_Evidence__c>();
        final List<Risk_Evidence__c> evidenceUpdates = new List<Risk_Evidence__c>();
        final List<Task> taskUpdates = new List<Task>();

        Changes(Datetime now) {
            this.now = now;
            List<TaskStatus> opening = [SELECT ApiName FROM TaskStatus WHERE IsDefault = true WITH SYSTEM_MODE LIMIT 1];
            List<TaskStatus> closing = [SELECT ApiName FROM TaskStatus WHERE IsClosed = true WITH SYSTEM_MODE ORDER BY SortOrder LIMIT 1];
            openStatus = opening.isEmpty() ? 'Not Started' : opening[0].ApiName;
            closedStatus = closing.isEmpty() ? 'Completed' : closing[0].ApiName;
        }

        void open(Account account, String customer, String activeKey, Finding finding, Id contactId) {
            Rule rule = finding.rule;
            newEpisodes.add(new Risk_Episode__c(
                Episode_Key__c = (activeKey + ':' + now.formatGmt('yyyyMMdd\'T\'HHmmssSSS\'Z\'')).left(140),
                Active_Key__c = activeKey, Account__c = account.Id, Risk_Rule__c = rule.record.Id,
                Rule_Code__c = rule.code, Rule_Version__c = rule.version, Status__c = 'active', Opened_At__c = now,
                Last_Evaluated_At__c = now, Explanation__c = explanation(finding),
                Evidence_Count__c = finding.supports.size(), OwnerId = ownerOf(rule)
            ));
            newFindings.add(finding);
            newContacts.add(contactId);
            newSubjects.add(('Risk warning: ' + rule.record.Name + ' – ' + account.Name + ' (' + customer + ')').left(255));
        }

        void refresh(Risk_Episode__c episode, Finding finding, Map<String, Risk_Evidence__c> rows, Task task) {
            Rule rule = finding.rule;
            String text = explanation(finding);
            Boolean explained = !same(episode.Explanation__c, text);
            updates.add(new Risk_Episode__c(
                Id = episode.Id, Risk_Rule__c = rule.record.Id, Rule_Version__c = rule.version,
                Explanation__c = text, Evidence_Count__c = finding.supports.size(), Last_Evaluated_At__c = now
            ));
            syncEvidence(episode, finding.supports, rows);
            if (explained && task != null && !task.IsClosed) {
                taskUpdates.add(new Task(Id = task.Id, Description = taskDescription(text)));
            }
        }

        /** Returns true when an open task was completed. */
        Boolean clear(Risk_Episode__c episode, String reason, Map<String, Risk_Evidence__c> rows, Task task) {
            updates.add(new Risk_Episode__c(
                Id = episode.Id, Status__c = 'cleared', Active_Key__c = null, Cleared_At__c = now,
                Clear_Reason__c = reason.left(255), Last_Evaluated_At__c = now
            ));
            syncEvidence(episode, new List<Support>(), rows);
            if (task == null || task.IsClosed) {
                return false;
            }
            String note = 'NABD cleared this warning on ' + stamp(now) + ': ' + reason
                + ' No follow-up is required unless you still want to contact the customer.';
            String before = task.Description == null ? '' : task.Description + '\n\n';
            taskUpdates.add(new Task(Id = task.Id, Status = closedStatus, Description = (before + note).left(32000)));
            return true;
        }

        void syncEvidence(Risk_Episode__c episode, List<Support> supports, Map<String, Risk_Evidence__c> rows) {
            Set<String> wanted = new Set<String>();
            for (Support support : supports) {
                String key = (episode.Episode_Key__c + ':' + support.key).left(160);
                wanted.add(key);
                Risk_Evidence__c row = rows == null ? null : rows.get(key);
                if (row == null) {
                    evidenceInserts.add(evidenceRow(episode.Id, key, support));
                } else if (!row.Current__c || !same(row.Detail__c, support.detail)) {
                    evidenceUpdates.add(new Risk_Evidence__c(Id = row.Id, Current__c = true, Detail__c = support.detail,
                                                             Observed_At__c = support.observedAt));
                }
            }
            for (Risk_Evidence__c row : rows == null ? new List<Risk_Evidence__c>() : rows.values()) {
                if (row.Current__c && !wanted.contains(row.Evidence_Key__c)) {
                    evidenceUpdates.add(new Risk_Evidence__c(Id = row.Id, Current__c = false)); // kept as history
                }
            }
        }

        Integer commitAll() {
            if (!newEpisodes.isEmpty()) {
                insert as system newEpisodes;
            }
            List<Task> newTasks = new List<Task>();
            for (Integer i = 0; i < newEpisodes.size(); i++) {
                Risk_Episode__c episode = newEpisodes[i];
                Finding finding = newFindings[i];
                syncEvidence(episode, finding.supports, null);
                newTasks.add(new Task(
                    Subject = newSubjects[i],
                    Description = taskDescription(episode.Explanation__c), Status = openStatus,
                    Priority = HIGH_UNRESOLVED.equals(finding.rule.code) ? 'High' : 'Normal',
                    ActivityDate = now.dateGmt().addDays(1), WhatId = episode.Id, WhoId = newContacts[i],
                    OwnerId = ownerOf(finding.rule), NABD_Task_Key__c = episode.Episode_Key__c.left(80),
                    NABD_Follow_Up_Type__c = 'risk_warning'
                ));
            }
            if (!newTasks.isEmpty()) {
                insert as system newTasks;
                for (Integer i = 0; i < newEpisodes.size(); i++) {
                    updates.add(new Risk_Episode__c(Id = newEpisodes[i].Id, Follow_Up_Task_Id__c = newTasks[i].Id));
                }
            }
            if (!evidenceInserts.isEmpty()) {
                insert as system evidenceInserts;
            }
            if (!evidenceUpdates.isEmpty()) {
                update as system evidenceUpdates;
            }
            if (!updates.isEmpty()) {
                update as system updates;
            }
            if (!taskUpdates.isEmpty()) {
                update as system taskUpdates;
            }
            return newTasks.size();
        }
    }

    static Risk_Evidence__c evidenceRow(Id episodeId, String key, Support support) {
        return new Risk_Evidence__c(
            Risk_Episode__c = episodeId, Evidence_Key__c = key, Case__c = support.caseId,
            Complaint_Evidence__c = support.evidenceId, Detail__c = support.detail,
            Observed_At__c = support.observedAt, Current__c = true
        );
    }

    static String taskDescription(String explanation) {
        return ('NABD risk warning. Review the customer\'s cases and decide whether to contact them. The '
                + 'warning updates itself and clears when the rule no longer holds.\n\n' + explanation).left(32000);
    }

    // --- Sweep ---------------------------------------------------------------------------

    /**
     * One page of the hourly sweep. Phase 0: customers with an open NABD case or one opened or
     * reopened within the longest active window. Phase 1: customers with an active warning. Each
     * phase pages by account ID in SOQL order; a customer in both is evaluated twice, harmlessly.
     */
    public static List<Id> sweepPage(Integer phase, Id after, Integer size, Datetime now) {
        Datetime start = now.addDays(-loadRules().maxWindowDays);
        List<Account> rows;
        if (phase == 0) {
            rows = after == null
                ? [SELECT Id FROM Account WHERE Id IN (
                       SELECT AccountId FROM Case WHERE NABD_Issue_Id__c != null
                       AND (IsClosed = false OR CreatedDate >= :start OR Last_Reopened_At__c >= :start))
                   WITH SYSTEM_MODE ORDER BY Id LIMIT :size]
                : [SELECT Id FROM Account WHERE Id > :after AND Id IN (
                       SELECT AccountId FROM Case WHERE NABD_Issue_Id__c != null
                       AND (IsClosed = false OR CreatedDate >= :start OR Last_Reopened_At__c >= :start))
                   WITH SYSTEM_MODE ORDER BY Id LIMIT :size];
        } else {
            rows = after == null
                ? [SELECT Id FROM Account WHERE Id IN (SELECT Account__c FROM Risk_Episode__c WHERE Active_Key__c != null)
                   WITH SYSTEM_MODE ORDER BY Id LIMIT :size]
                : [SELECT Id FROM Account WHERE Id > :after
                   AND Id IN (SELECT Account__c FROM Risk_Episode__c WHERE Active_Key__c != null)
                   WITH SYSTEM_MODE ORDER BY Id LIMIT :size];
        }
        List<Id> ids = new List<Id>();
        for (Account row : rows) {
            ids.add(row.Id);
        }
        return ids;
    }

    /** The sweep reached every relevant customer: stamp the rule versions in use (data freshness). */
    public static void finishSweep(Datetime startedAt) {
        saveRuleState(loadRules(), startedAt);
    }

    // --- Helpers -----------------------------------------------------------------------

    static Integer streak(Case item) {
        return item.Negative_Streak__c == null ? 0 : item.Negative_Streak__c.intValue();
    }

    static Datetime openSince(Case item) {
        if (item.NABD_Open_Since__c != null) {
            return item.NABD_Open_Since__c;
        }
        return item.Last_Reopened_At__c != null && item.Last_Reopened_At__c > item.CreatedDate
            ? item.Last_Reopened_At__c : item.CreatedDate;
    }

    static String caseLine(Case item) {
        return 'Case ' + item.CaseNumber + ' · ' + categoryLabel(item.Complaint_Category__c) + ' · '
            + (item.Priority == null ? 'no priority' : item.Priority) + ' · opened ' + stamp(item.CreatedDate)
            + ' · ' + item.Status;
    }

    static Map<String, String> categoryLabels;

    static String categoryLabel(String value) {
        if (categoryLabels == null) {
            categoryLabels = new Map<String, String>();
            for (Schema.PicklistEntry entry : Case.Complaint_Category__c.getDescribe().getPicklistValues()) {
                categoryLabels.put(entry.getValue(), entry.getLabel());
            }
        }
        return value == null ? 'no category' : (categoryLabels.containsKey(value) ? categoryLabels.get(value) : value);
    }

    static String stamp(Datetime value) {
        return value == null ? 'unknown time' : value.formatGmt('yyyy-MM-dd HH:mm') + ' UTC';
    }

    static Id ownerOf(Rule rule) {
        Id owner = rule.record.OwnerId;
        // Tasks need a user; a rule owned by a queue falls back to whoever runs the evaluation.
        return owner != null && String.valueOf(owner).startsWith('005') ? owner : UserInfo.getUserId();
    }

    static Id toId(String value) {
        if (String.isBlank(value)) {
            return null;
        }
        try {
            return Id.valueOf(value);
        } catch (Exception error) {
            return null;
        }
    }

    static Boolean same(String left, String right) {
        return left == null ? right == null : left.equals(right);
    }
}
