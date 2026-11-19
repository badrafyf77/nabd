/**
 * The risk engine's rules and warning lifecycle (Phase 6): thresholds, windows
 * and streaks; one warning and one task under repeated evaluation; clearing,
 * history and recurrence; rule versions and configuration errors; and the
 * trigger, gateway and hourly-sweep paths. Most tests call the engine with a
 * fixed time; CreatedDate is set relative to it.
 */
@IsTest
private class NabdRiskEngineTest {
    static final String CUSTOMER = 'C951';
    static final Datetime NOW = System.now();

    @TestSetup
    static void data() {
        Account account = new Account(Name = 'Risk Test', ShopEasy_Customer_Id__c = CUSTOMER);
        insert as system account;
        Contact person = new Contact(FirstName = 'Risk', LastName = 'Test', AccountId = account.Id,
                                     ShopEasy_Customer_Id__c = CUSTOMER);
        insert as system person;
        List<Risk_Rule__c> rules = new List<Risk_Rule__c>{
            rule('repeat_complaints', 1, '{"distinct_complaints": 3, "window_days": 30}'),
            rule('high_priority_unresolved', 1, '{"priority": "High", "unresolved_hours": 24}'),
            rule('persistent_negative_sentiment', 1, '{"follow_ups": 1}')
        };
        insert as system rules;
    }

    // --- Helpers -------------------------------------------------------------------

    static Risk_Rule__c rule(String code, Integer version, String parameters) {
        return new Risk_Rule__c(Name = code.replace('_', ' ').capitalize(), Rule_Code__c = code, Version__c = version,
                                Active__c = true, Parameters_JSON__c = parameters);
    }

    static Id accountId() {
        return [SELECT Id FROM Account WHERE ShopEasy_Customer_Id__c = :CUSTOMER WITH SYSTEM_MODE].Id;
    }

    static Integer issues = 0;

    static Case newCase(String priority, Integer hoursAgo) {
        issues++;
        Case item = new Case(AccountId = accountId(), Subject = 'Risk test ' + issues, Status = 'New',
                             Priority = priority, Origin = 'NABD Chat', Complaint_Category__c = 'Delivery',
                             NABD_Issue_Id__c = 'risk-test-issue-' + issues, Sentiment__c = 'neutral',
                             Negative_Streak__c = 0);
        insert as system item;
        if (hoursAgo != null) { // null keeps the real creation time (setCreatedDate is refused after startTest)
            Test.setCreatedDate(item.Id, NOW.addHours(-hoursAgo));
        }
        return item;
    }

    static NabdRiskEngine.Summary evaluate(Datetime at) {
        return NabdRiskEngine.evaluate(new Set<Id>{accountId()}, at);
    }

    static List<Risk_Episode__c> episodes(String code) {
        return [SELECT Id, Episode_Key__c, Active_Key__c, Status__c, Rule_Version__c, Risk_Rule__c, Explanation__c,
                       Evidence_Count__c, Follow_Up_Task_Id__c, Cleared_At__c, Clear_Reason__c, OwnerId
                FROM Risk_Episode__c WHERE Account__c = :accountId() AND Rule_Code__c = :code
                WITH SYSTEM_MODE ORDER BY Opened_At__c, Name];
    }

    static List<Risk_Episode__c> active(String code) {
        List<Risk_Episode__c> result = new List<Risk_Episode__c>();
        for (Risk_Episode__c episode : episodes(code)) {
            if (episode.Active_Key__c != null) {
                result.add(episode);
            }
        }
        return result;
    }

    static List<Task> tasksFor(Id episodeId) {
        return [SELECT Id, Status, IsClosed, Priority, OwnerId, WhoId, Description, NABD_Task_Key__c,
                       NABD_Follow_Up_Type__c
                FROM Task WHERE WhatId = :episodeId WITH SYSTEM_MODE];
    }

    static Integer currentEvidence(Id episodeId) {
        return [SELECT COUNT() FROM Risk_Evidence__c WHERE Risk_Episode__c = :episodeId AND Current__c = true
                WITH SYSTEM_MODE];
    }

    // --- Repeated complaints -------------------------------------------------------------

    @IsTest
    static void threeDistinctComplaintsOpenOneWarningWithOneTask() {
        newCase('Low', 48);
        newCase('Low', 24);
        evaluate(NOW);
        System.assertEquals(0, active('repeat_complaints').size());
        newCase('Low', 1);
        NabdRiskEngine.Summary summary = evaluate(NOW);
        System.assertEquals(1, summary.opened);
        System.assertEquals(1, summary.tasksCreated);
        List<Risk_Episode__c> found = active('repeat_complaints');
        System.assertEquals(1, found.size());
        Risk_Episode__c episode = found[0];
        System.assertEquals(CUSTOMER + ':repeat_complaints', episode.Active_Key__c);
        System.assert(episode.Episode_Key__c.startsWith(CUSTOMER + ':repeat_complaints:'), episode.Episode_Key__c);
        System.assertEquals(3, episode.Evidence_Count__c.intValue());
        System.assert(episode.Explanation__c.contains('3 distinct complaints'), episode.Explanation__c);
        System.assert(episode.Explanation__c.contains('version 1'), episode.Explanation__c);
        System.assertEquals(3, currentEvidence(episode.Id));
        List<Task> tasks = tasksFor(episode.Id);
        System.assertEquals(1, tasks.size());
        System.assertEquals(String.valueOf(tasks[0].Id), episode.Follow_Up_Task_Id__c);
        System.assertEquals('risk_warning', tasks[0].NABD_Follow_Up_Type__c);
        System.assertEquals(episode.Episode_Key__c.left(80), tasks[0].NABD_Task_Key__c);
        System.assertEquals(UserInfo.getUserId(), tasks[0].OwnerId); // the rule's owner
        System.assertEquals(UserInfo.getUserId(), episode.OwnerId);
        System.assertNotEquals(null, tasks[0].WhoId);
        System.assertEquals(false, tasks[0].IsClosed);
        System.assertEquals('Normal', tasks[0].Priority);

        // Repeated evaluation updates the same warning: no second warning, task or evidence row.
        NabdRiskEngine.Summary again = evaluate(NOW.addHours(1));
        System.assertEquals(0, again.opened);
        System.assertEquals(1, again.updated);
        System.assertEquals(1, episodes('repeat_complaints').size());
        System.assertEquals(1, tasksFor(episode.Id).size());
        System.assertEquals(3, [SELECT COUNT() FROM Risk_Evidence__c WHERE Risk_Episode__c = :episode.Id WITH SYSTEM_MODE]);
    }

    @IsTest
    static void duplicatesAndOlderComplaintsDoNotCountButAReopeningDoes() {
        Case old = newCase('Low', 24 * 40);
        newCase('Low', 24);
        Case third = newCase('Low', 2);
        evaluate(NOW);
        System.assertEquals(0, active('repeat_complaints').size()); // only two in the last 30 days

        Case reopened = new Case(Id = old.Id, Status = 'Working', Last_Reopened_At__c = NOW.addDays(-5), Reopen_Count__c = 1);
        update as system reopened;
        evaluate(NOW);
        Risk_Episode__c episode = active('repeat_complaints')[0];
        System.assert(episode.Explanation__c.contains('reopened'), episode.Explanation__c);

        Case duplicate = new Case(Id = third.Id, Status = 'Closed', Resolution_Outcome__c = 'duplicate');
        update as system duplicate;
        evaluate(NOW);
        System.assertEquals(0, active('repeat_complaints').size()); // two distinct issues remain
    }

    @IsTest
    static void aClearedWarningKeepsItsHistoryAndARecurrenceOpensANewOne() {
        newCase('Low', 30);
        newCase('Low', 20);
        Case third = newCase('Low', 10);
        evaluate(NOW);
        Risk_Episode__c first = active('repeat_complaints')[0];
        Id firstTask = tasksFor(first.Id)[0].Id;

        Case duplicate = new Case(Id = third.Id, Status = 'Closed', Resolution_Outcome__c = 'duplicate');
        update as system duplicate;
        NabdRiskEngine.Summary cleared = evaluate(NOW.addHours(1));
        System.assertEquals(1, cleared.cleared);
        System.assertEquals(1, cleared.tasksCompleted);
        Risk_Episode__c after = episodes('repeat_complaints')[0];
        System.assertEquals('cleared', after.Status__c);
        System.assertEquals(null, after.Active_Key__c);
        System.assertNotEquals(null, after.Cleared_At__c);
        System.assert(after.Clear_Reason__c.contains('Fewer than 3'), after.Clear_Reason__c);
        System.assertEquals(0, currentEvidence(first.Id));
        System.assertEquals(3, [SELECT COUNT() FROM Risk_Evidence__c WHERE Risk_Episode__c = :first.Id WITH SYSTEM_MODE]);
        Task done = [SELECT IsClosed, Description FROM Task WHERE Id = :firstTask WITH SYSTEM_MODE];
        System.assertEquals(true, done.IsClosed);
        System.assert(done.Description.contains('NABD cleared this warning'), done.Description);

        newCase('Low', 1);
        evaluate(NOW.addHours(2));
        List<Risk_Episode__c> all = episodes('repeat_complaints');
        System.assertEquals(2, all.size());
        Risk_Episode__c second = active('repeat_complaints')[0];
        System.assertNotEquals(first.Id, second.Id);
        System.assertNotEquals(first.Episode_Key__c, second.Episode_Key__c);
        System.assertEquals(1, tasksFor(second.Id).size());
        System.assertEquals(false, tasksFor(second.Id)[0].IsClosed);
        System.assertEquals('cleared', [SELECT Status__c FROM Risk_Episode__c WHERE Id = :first.Id WITH SYSTEM_MODE].Status__c);
    }

    @IsTest
    static void aTaskStaffCompletedIsNeverRecreated() {
        newCase('Low', 30);
        newCase('Low', 20);
        newCase('Low', 10);
        evaluate(NOW);
        Risk_Episode__c episode = active('repeat_complaints')[0];
        Task task = tasksFor(episode.Id)[0];
        Task completed = new Task(Id = task.Id, Status = 'Completed', Description = 'Called the customer.');
        update as system completed;

        newCase('Low', 1);
        evaluate(NOW.addHours(1));
        Risk_Episode__c updated = active('repeat_complaints')[0];
        System.assertEquals(episode.Id, updated.Id);
        System.assertEquals(4, updated.Evidence_Count__c.intValue());
        System.assertEquals(4, currentEvidence(episode.Id));
        List<Task> tasks = tasksFor(episode.Id);
        System.assertEquals(1, tasks.size());
        System.assertEquals('Called the customer.', tasks[0].Description); // staff's closed task is left alone
    }

    // --- High priority unresolved ------------------------------------------------------

    @IsTest
    static void aHighPriorityComplaintWarnsAfterTwentyFourHoursAndClearsWhenStaffAct() {
        Case item = newCase('High', 23);
        evaluate(NOW);
        System.assertEquals(0, active('high_priority_unresolved').size());
        evaluate(NOW.addHours(2));
        Risk_Episode__c episode = active('high_priority_unresolved')[0];
        System.assert(episode.Explanation__c.contains('25 hours'), episode.Explanation__c);
        System.assertEquals('High', tasksFor(episode.Id)[0].Priority);

        Case lowered = new Case(Id = item.Id, Priority = 'Medium'); // staff decided it is less urgent
        update as system lowered;
        evaluate(NOW.addHours(3));
        System.assertEquals(0, active('high_priority_unresolved').size());
        System.assertEquals(true, tasksFor(episode.Id)[0].IsClosed);
    }

    @IsTest
    static void aReopenedHighPriorityComplaintCountsFromTheReopening() {
        Case item = newCase('High', 72);
        Case reopened = new Case(Id = item.Id, Status = 'Working', Last_Reopened_At__c = NOW.addHours(-2), Reopen_Count__c = 1);
        update as system reopened;
        evaluate(NOW);
        System.assertEquals(0, active('high_priority_unresolved').size());
        evaluate(NOW.addHours(23));
        System.assertEquals(1, active('high_priority_unresolved').size());
        Case closed = new Case(Id = item.Id, Status = 'Closed', Resolution_Outcome__c = 'resolved');
        update as system closed;
        evaluate(NOW.addHours(24));
        System.assertEquals(0, active('high_priority_unresolved').size());
    }

    // --- Persistent negative sentiment ---------------------------------------------------

    @IsTest
    static void negativeSentimentAtTheNextFollowUpWarnsAndCitesTheCustomerEvidence() {
        Case item = newCase('Low', 5);
        List<Complaint_Evidence__c> rows = new List<Complaint_Evidence__c>{
            evidence(item.Id, 'msg-1', 'customer_report', 'negative', NOW.addHours(-5)),
            evidence(item.Id, 'fact-1', 'verified_fact', null, NOW.addHours(-5)),
            evidence(item.Id, 'msg-2', 'customer_report', 'very_negative', NOW.addHours(-1))
        };
        insert as system rows;
        Case one = new Case(Id = item.Id, Negative_Streak__c = 1);
        update as system one;
        evaluate(NOW);
        System.assertEquals(0, active('persistent_negative_sentiment').size());

        Case two = new Case(Id = item.Id, Negative_Streak__c = 2);
        update as system two;
        evaluate(NOW);
        Risk_Episode__c episode = active('persistent_negative_sentiment')[0];
        System.assertEquals(3, episode.Evidence_Count__c.intValue()); // the case and its two negative contacts
        System.assert(episode.Explanation__c.contains('consecutive'), episode.Explanation__c);
        Set<Id> cited = new Set<Id>();
        for (Risk_Evidence__c row : [SELECT Complaint_Evidence__c FROM Risk_Evidence__c
                                     WHERE Risk_Episode__c = :episode.Id AND Complaint_Evidence__c != null WITH SYSTEM_MODE]) {
            cited.add(row.Complaint_Evidence__c);
        }
        System.assertEquals(new Set<Id>{rows[0].Id, rows[2].Id}, cited);

        Case closed = new Case(Id = item.Id, Status = 'Closed', Resolution_Outcome__c = 'resolved');
        update as system closed;
        evaluate(NOW);
        System.assertEquals(0, active('persistent_negative_sentiment').size());
    }

    static Complaint_Evidence__c evidence(Id caseId, String ref, String kind, String sentiment, Datetime at) {
        return new Complaint_Evidence__c(Case__c = caseId, Evidence_Key__c = String.valueOf(caseId) + ':' + ref,
                                         Source_Message_Id__c = ref, Evidence_Kind__c = kind,
                                         Original_Text__c = 'Test text ' + ref, Assessed_Sentiment__c = sentiment,
                                         Captured_At__c = at);
    }

    @IsTest
    static void gatewayFollowUpsReachTheEngineThroughARiskCheck() {
        Test.startTest();
        Map<String, Object> created = post(new Map<String, Object>{
            'operationId' => 'op-risk-create-01', 'operation' => 'create_case', 'customerId' => CUSTOMER,
            'issueId' => 'nabd-issue-risk-0001', 'category' => 'Delivery', 'subject' => 'Parcel lost',
            'summary' => 'Customer says the parcel is lost.', 'language' => 'en', 'sentiment' => 'negative',
            'urgency' => 'medium', 'confidence' => 0.9, 'needsReview' => false, 'priority' => 'Medium',
            'priorityReason' => 'Medium: lost parcel reported',
            'evidence' => new List<Object>{contact('msg-risk-1', 'Where is my parcel?')}
        });
        System.assertEquals('confirmed', created.get('status'), String.valueOf(created));
        Id caseId = (Id) (String) created.get('caseId');
        Case current = [SELECT SystemModstamp FROM Case WHERE Id = :caseId WITH SYSTEM_MODE];
        Map<String, Object> followUp = post(new Map<String, Object>{
            'operationId' => 'op-risk-update-01', 'operation' => 'update_case', 'customerId' => CUSTOMER,
            'caseId' => caseId, 'expectedVersion' => NabdCaseCommands.version(current.SystemModstamp),
            'sentiment' => 'very_negative',
            'evidence' => new List<Object>{contact('msg-risk-2', 'Still nothing. This is unacceptable.')}
        });
        System.assertEquals('confirmed', followUp.get('status'), String.valueOf(followUp));
        Test.stopTest(); // delivers the risk checks the case trigger published after each command
        Test.getEventBus().deliver();

        System.assertEquals(2, [SELECT Negative_Streak__c FROM Case WHERE Id = :caseId WITH SYSTEM_MODE].Negative_Streak__c.intValue());
        List<Risk_Episode__c> found = active('persistent_negative_sentiment');
        System.assertEquals(1, found.size());
        System.assertEquals(1, tasksFor(found[0].Id).size());
    }

    static Map<String, Object> contact(String ref, String text) {
        return new Map<String, Object>{
            'ref' => ref, 'kind' => 'customer_report', 'text' => text, 'language' => 'en', 'script' => 'latin',
            'capturedAt' => '2026-11-18T10:00:00.000Z'
        };
    }

    static Map<String, Object> post(Map<String, Object> command) {
        RestRequest request = new RestRequest();
        request.requestURI = '/services/apexrest/nabd/v1/case-commands';
        request.httpMethod = 'POST';
        request.requestBody = Blob.valueOf(JSON.serialize(command));
        RestContext.request = request;
        RestContext.response = new RestResponse();
        NabdCaseCommands.post();
        return (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
    }

    @IsTest
    static void caseChangesPublishARiskCheckThatEvaluatesTheCustomer() {
        Test.startTest();
        newCase('Low', null);
        newCase('Low', null);
        newCase('Low', null);
        Test.stopTest();
        Test.getEventBus().deliver();
        System.assertEquals(1, active('repeat_complaints').size());
        // A change the rules do not read publishes nothing more and changes nothing.
        Case item = [SELECT Id FROM Case WHERE AccountId = :accountId() WITH SYSTEM_MODE LIMIT 1];
        Case unrelated = new Case(Id = item.Id, Subject = 'Renamed by staff');
        update as system unrelated;
        Test.getEventBus().deliver();
        System.assertEquals(1, episodes('repeat_complaints').size());
    }

    @IsTest
    static void aRiskCheckForAnUnknownOrMissingCustomerIsHarmless() {
        EventBus.publish(new List<NABD_Risk_Check__e>{
            new NABD_Risk_Check__e(Account_Id__c = 'not-an-id'),
            new NABD_Risk_Check__e(Account_Id__c = accountId())
        });
        Test.getEventBus().deliver();
        System.assertEquals(0, [SELECT COUNT() FROM Risk_Episode__c WITH SYSTEM_MODE]);
    }

    // --- Rules, versions and configuration -----------------------------------------------

    @IsTest
    static void aNewRuleVersionTakesOverTheSameWarning() {
        newCase('Low', 30);
        newCase('Low', 20);
        newCase('Low', 10);
        evaluate(NOW);
        Risk_Episode__c episode = active('repeat_complaints')[0];

        Risk_Rule__c v2 = rule('repeat_complaints', 2, '{"distinct_complaints": 2, "window_days": 14}');
        insert as system v2;
        System.assertEquals('repeat_complaints@2', [SELECT Rule_Key__c FROM Risk_Rule__c WHERE Id = :v2.Id WITH SYSTEM_MODE].Rule_Key__c);
        Risk_Rule__c v1 = [SELECT Id FROM Risk_Rule__c WHERE Rule_Key__c = 'repeat_complaints@1' WITH SYSTEM_MODE];
        v1.Active__c = false;
        update as system v1;
        evaluate(NOW.addHours(1));
        Risk_Episode__c updated = active('repeat_complaints')[0];
        System.assertEquals(episode.Id, updated.Id);
        System.assertEquals(2, updated.Rule_Version__c.intValue());
        System.assertEquals(v2.Id, updated.Risk_Rule__c);
        System.assert(updated.Explanation__c.contains('version 2'), updated.Explanation__c);
        System.assertEquals(1, tasksFor(episode.Id).size());

        v2.Active__c = false;
        update as system v2;
        evaluate(NOW.addHours(2));
        Risk_Episode__c cleared = episodes('repeat_complaints')[0];
        System.assertEquals('cleared', cleared.Status__c);
        System.assert(cleared.Clear_Reason__c.contains('no longer active'), cleared.Clear_Reason__c);
    }

    @IsTest
    static void invalidRulesAreRefusedAndTheNewestActiveVersionWins() {
        for (Risk_Rule__c bad : new List<Risk_Rule__c>{
            rule('unknown_rule', 1, '{}'),
            rule('repeat_complaints', 3, '{"distinct_complaints": 3, "window_days": "thirty"}'),
            rule('high_priority_unresolved', 2, '{"priority": "Urgent", "unresolved_hours": 24}'),
            rule('persistent_negative_sentiment', 2, '[1, 2]')
        }) {
            try {
                insert as system bad;
                System.assert(false, 'Expected the rule to be refused: ' + bad.Rule_Code__c);
            } catch (DmlException error) {
                System.assert(error.getMessage().contains('must'), error.getMessage());
            }
        }
        Risk_Rule__c v2 = rule('repeat_complaints', 2, '{"distinct_complaints": 4, "window_days": 30}');
        insert as system v2;
        NabdRiskEngine.Rules rules = NabdRiskEngine.loadRules();
        System.assertEquals(2, rules.usable.get('repeat_complaints').version);
        NabdRiskEngine.saveRuleState(rules, null);
        Risk_Rule__c v1 = [SELECT Last_Error__c FROM Risk_Rule__c WHERE Rule_Key__c = 'repeat_complaints@1' WITH SYSTEM_MODE];
        System.assert(v1.Last_Error__c.contains('version 2'), v1.Last_Error__c);
    }

    @IsTest
    static void aRuleThatBecameInvalidReportsAnErrorAndLeavesItsWarningsAlone() {
        newCase('Low', 30);
        newCase('Low', 20);
        newCase('Low', 10);
        evaluate(NOW);
        Risk_Episode__c episode = active('repeat_complaints')[0];
        NabdRiskEvaluation.checkRules = false; // as if saved before a stricter check existed
        Risk_Rule__c v1 = [SELECT Id FROM Risk_Rule__c WHERE Rule_Key__c = 'repeat_complaints@1' WITH SYSTEM_MODE];
        v1.Parameters_JSON__c = '{"distinct_complaints": 0, "window_days": 30}';
        update as system v1;
        NabdRiskEngine.Summary summary = evaluate(NOW.addHours(1));
        System.assertEquals(0, summary.cleared); // a configuration error never means "the rule no longer holds"
        System.assertEquals(episode.Id, active('repeat_complaints')[0].Id);
        String error = [SELECT Last_Error__c FROM Risk_Rule__c WHERE Id = :v1.Id WITH SYSTEM_MODE].Last_Error__c;
        System.assert(error.contains('distinct_complaints'), error);
    }

    // --- Sweep and schedule --------------------------------------------------------------

    @IsTest
    static void theHourlySweepFindsAgingComplaintsAndRecordsFreshness() {
        newCase('High', 30);
        Test.startTest();
        System.enqueueJob(NabdRiskEvaluation.sweep());
        Test.stopTest();
        while (!NabdRiskEvaluation.chainedInTest.isEmpty()) {
            NabdRiskEvaluation.chainedInTest.remove(0).execute(null);
        }
        System.assertEquals(1, active('high_priority_unresolved').size());
        System.assertEquals(3, [SELECT COUNT() FROM Risk_Rule__c WHERE Last_Evaluated_At__c != null WITH SYSTEM_MODE]);
        List<Id> page = NabdRiskEngine.sweepPage(1, null, 10, System.now());
        System.assertEquals(new List<Id>{accountId()}, page); // the active warning keeps the customer in sweeps
    }

    @IsTest
    static void theScheduleExistsOnceAndQueuesASweep() {
        Test.startTest();
        Id first = NabdRiskSchedule.ensure();
        System.assertEquals(first, NabdRiskSchedule.ensure());
        new NabdRiskSchedule().execute(null);
        System.assertEquals(1, Limits.getQueueableJobs());
        Test.stopTest();
        CronTrigger job = [SELECT CronExpression, CronJobDetail.Name FROM CronTrigger WHERE Id = :first WITH SYSTEM_MODE];
        System.assertEquals(NabdRiskSchedule.JOB_NAME, job.CronJobDetail.Name);
        System.assertEquals(NabdRiskSchedule.CRON, job.CronExpression);
    }
}
