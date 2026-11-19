/**
 * The case command gateway's rules: idempotent receipts, customer ownership,
 * versions, lifecycle transitions, staff overrides and evidence de-duplication.
 * Staff edits are simulated with system-mode DML between commands.
 */
@IsTest
private class NabdCaseCommandsTest {
    static final String CUSTOMER = 'C901';

    @TestSetup
    static void data() {
        Account mine = new Account(Name = 'Sara Test', ShopEasy_Customer_Id__c = CUSTOMER);
        Account other = new Account(Name = 'Other Test', ShopEasy_Customer_Id__c = 'C902');
        List<Account> accounts = new List<Account>{mine, other};
        insert as system accounts;
        Contact contact = new Contact(FirstName = 'Sara', LastName = 'Test', AccountId = mine.Id,
                                      ShopEasy_Customer_Id__c = CUSTOMER);
        insert as system contact;
        List<ShopEasy_Order__c> orders = new List<ShopEasy_Order__c>{
            new ShopEasy_Order__c(Name = 'SE-9901', Order_Number__c = 'SE-9901', Account__c = mine.Id,
                                  Order_Date__c = Date.today(), Status__c = 'shipped', Total_GBP__c = 10),
            new ShopEasy_Order__c(Name = 'SE-9902', Order_Number__c = 'SE-9902', Account__c = other.Id,
                                  Order_Date__c = Date.today(), Status__c = 'shipped', Total_GBP__c = 10)
        };
        insert as system orders;
    }

    // --- Helpers -------------------------------------------------------------------

    static Map<String, Object> post(String body) {
        RestRequest request = new RestRequest();
        request.requestURI = '/services/apexrest/nabd/v1/case-commands';
        request.httpMethod = 'POST';
        request.requestBody = Blob.valueOf(body);
        RestContext.request = request;
        RestContext.response = new RestResponse();
        NabdCaseCommands.post();
        System.assertEquals(200, RestContext.response.statusCode);
        return (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
    }

    static Map<String, Object> post(Map<String, Object> command) {
        return post(JSON.serialize(command));
    }

    static Map<String, Object> lookup(String operationId, String customerId) {
        RestRequest request = new RestRequest();
        request.requestURI = '/services/apexrest/nabd/v1/case-commands/' + operationId;
        request.httpMethod = 'GET';
        request.params.put('customerId', customerId);
        RestContext.request = request;
        RestContext.response = new RestResponse();
        NabdCaseCommands.get();
        return (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
    }

    static Map<String, Object> evidence(String ref, String kind, String text) {
        Map<String, Object> item = new Map<String, Object>{
            'ref' => ref, 'kind' => kind, 'text' => text, 'language' => 'en', 'script' => 'latin',
            'capturedAt' => '2026-11-18T10:00:00.000Z'
        };
        if (kind == 'verified_fact') {
            item.put('verifiedSource', 'ShopEasy_Delivery__c:a0X000000000001@2026-11-18T09:00:00.000Z');
        }
        return item;
    }

    static Map<String, Object> creation(String operationId, String issueId) {
        return new Map<String, Object>{
            'operationId' => operationId, 'operation' => 'create_case', 'customerId' => CUSTOMER,
            'conversationId' => 'conv-test', 'issueId' => issueId, 'orderNumber' => 'SE-9901',
            'category' => 'Delivery', 'subject' => 'Parcel SE-9901 lost', 'summary' => 'Customer says SE-9901 is lost.',
            'language' => 'en', 'sentiment' => 'negative', 'urgency' => 'medium', 'confidence' => 0.9,
            'needsReview' => false, 'priority' => 'Medium', 'priorityReason' => 'Medium: lost parcel reported',
            'evidence' => new List<Object>{
                evidence('msg-0001', 'customer_report', 'My parcel never arrived.'),
                evidence('fact:0001', 'verified_fact', 'Delivery SE-9901 has status Lost.')
            }
        };
    }

    static Map<String, Object> change(String operation, String operationId, Id caseId) {
        Case current = [SELECT SystemModstamp FROM Case WHERE Id = :caseId WITH SYSTEM_MODE];
        return new Map<String, Object>{
            'operationId' => operationId, 'operation' => operation, 'customerId' => CUSTOMER,
            'conversationId' => 'conv-test', 'caseId' => caseId,
            'expectedVersion' => NabdCaseCommands.version(current.SystemModstamp),
            'evidence' => new List<Object>{evidence('msg-0002', 'customer_report', 'Still nothing.')}
        };
    }

    static Id createCase(String operationId, String issueId) {
        Map<String, Object> result = post(creation(operationId, issueId));
        System.assertEquals('confirmed', result.get('status'), String.valueOf(result));
        return (Id) (String) result.get('caseId');
    }

    static Case reload(Id caseId) {
        return [SELECT Status, Priority, NABD_Assessed_Priority__c, English_Summary__c, NABD_Summary_Hash__c,
                       Sentiment__c, Reopen_Count__c, Resolution_Outcome__c, Resolution_Confirmation_Message__c,
                       Owner.Name, Needs_Review__c, Staff_Action_Required__c, Staff_Action_Completed__c,
                       ShopEasy_Order__c, Origin, NABD_Issue_Id__c, Description
                FROM Case WHERE Id = :caseId WITH SYSTEM_MODE];
    }

    static Integer receipts(String operationId) {
        return [SELECT COUNT() FROM NABD_Operation_Receipt__c WHERE Operation_Id__c = :operationId WITH SYSTEM_MODE];
    }

    // --- Tests -------------------------------------------------------------------------

    @IsTest
    static void createRoutesTheCaseAndStoresEvidenceAndReceipt() {
        Map<String, Object> result = post(creation('op-create-0001', 'nabd-issue-test-0001'));
        System.assertEquals('confirmed', result.get('status'));
        System.assertEquals(true, result.get('recorded'));
        System.assertEquals('NABD_Delivery', result.get('queue'));
        System.assertEquals('New', result.get('caseStatus'));
        System.assertEquals(2, result.get('evidenceAdded'));
        Case created = reload((Id) (String) result.get('caseId'));
        System.assertEquals('Medium', created.Priority);
        System.assertEquals('Medium', created.NABD_Assessed_Priority__c);
        System.assertEquals('NABD Chat', created.Origin);
        System.assertEquals('My parcel never arrived.', created.Description);
        System.assertEquals(NabdCaseCommands.hashOf(created.English_Summary__c), created.NABD_Summary_Hash__c);
        System.assertNotEquals(null, created.ShopEasy_Order__c);
        System.assertEquals(2, [SELECT COUNT() FROM Complaint_Evidence__c WHERE Case__c = :created.Id WITH SYSTEM_MODE]);
        System.assertEquals(1, receipts('op-create-0001'));
    }

    @IsTest
    static void aRepeatedCommandReturnsTheStoredResultWithoutASecondCase() {
        String body = JSON.serialize(creation('op-create-0002', 'nabd-issue-test-0002'));
        Map<String, Object> first = post(body);
        Map<String, Object> again = post(body);
        System.assertEquals(true, again.get('replayed'));
        System.assertEquals(first.get('caseId'), again.get('caseId'));
        System.assertEquals(1, [SELECT COUNT() FROM Case WHERE NABD_Issue_Id__c = 'nabd-issue-test-0002' WITH SYSTEM_MODE]);
        System.assertEquals(1, receipts('op-create-0002'));
        Map<String, Object> found = lookup('op-create-0002', CUSTOMER);
        System.assertEquals('confirmed', found.get('status'));
        System.assertEquals(first.get('caseNumber'), found.get('caseNumber'));
        System.assertEquals('unknown', lookup('op-create-0002', 'C902').get('status'));
        System.assertEquals('unknown', lookup('op-never-sent', CUSTOMER).get('status'));
    }

    @IsTest
    static void reusingAnOperationIdForAnotherRequestIsRefused() {
        post(creation('op-create-0003', 'nabd-issue-test-0003'));
        Map<String, Object> other = creation('op-create-0003', 'nabd-issue-test-0003');
        other.put('subject', 'Something else');
        Map<String, Object> result = post(other);
        System.assertEquals('failed', result.get('status'));
        System.assertEquals('operation_reused', result.get('reason'));
        System.assertEquals(false, result.get('recorded'));
        Map<String, Object> sameIssue = post(creation('op-create-0004', 'nabd-issue-test-0003'));
        System.assertEquals('conflict', sameIssue.get('status'));
        System.assertEquals('issue_exists', sameIssue.get('reason'));
    }

    @IsTest
    static void anotherCustomersOrderOrCaseIsRefused() {
        Map<String, Object> command = creation('op-create-0005', 'nabd-issue-test-0005');
        command.put('orderNumber', 'SE-9902');
        Map<String, Object> result = post(command);
        System.assertEquals('failed', result.get('status'));
        System.assertEquals('order_not_found', result.get('reason'));
        System.assertEquals(0, [SELECT COUNT() FROM Case WHERE NABD_Issue_Id__c = 'nabd-issue-test-0005' WITH SYSTEM_MODE]);

        Id caseId = createCase('op-create-0006', 'nabd-issue-test-0006');
        Map<String, Object> intruder = change('update_case', 'op-update-0006', caseId);
        intruder.put('customerId', 'C902');
        System.assertEquals('case_not_found', post(intruder).get('reason'));
        command = creation('op-create-0007', 'nabd-issue-test-0007');
        command.put('customerId', 'C999');
        System.assertEquals('unknown_customer', post(command).get('reason'));
    }

    @IsTest
    static void aStaleVersionIsAConflictAndChangesNothing() {
        Id caseId = createCase('op-create-0010', 'nabd-issue-test-0010');
        Map<String, Object> command = change('update_case', 'op-update-0010', caseId);
        command.put('expectedVersion', '2020-01-01T00:00:00.000Z');
        command.put('sentiment', 'very_negative');
        Map<String, Object> result = post(command);
        System.assertEquals('conflict', result.get('status'));
        System.assertEquals('version_changed', result.get('reason'));
        System.assertNotEquals(null, result.get('caseNumber'));
        System.assertEquals('negative', reload(caseId).Sentiment__c);
        System.assertEquals(1, receipts('op-update-0010'));
    }

    @IsTest
    static void updatesRespectStaffPriorityAndSummaryEdits() {
        Id caseId = createCase('op-create-0020', 'nabd-issue-test-0020');
        Case staffEdit = new Case(Id = caseId, Status = 'Waiting on Customer', Priority = 'Low',
                                  English_Summary__c = 'Staff wrote this summary.');
        update as system staffEdit;
        Map<String, Object> command = change('update_case', 'op-update-0020', caseId);
        command.put('priority', 'High');
        command.put('priorityReason', 'High: suspected duplicate charge');
        command.put('summary', 'NABD would write this.');
        command.put('sentiment', 'very_negative');
        Map<String, Object> result = post(command);
        System.assertEquals('confirmed', result.get('status'));
        List<Object> overrides = (List<Object>) result.get('overrides');
        System.assert(overrides.contains('priority') && overrides.contains('summary'), String.valueOf(overrides));
        Case current = reload(caseId);
        System.assertEquals('Working', current.Status);
        System.assertEquals('Low', current.Priority);
        System.assertEquals('Staff wrote this summary.', current.English_Summary__c);
        System.assertEquals('very_negative', current.Sentiment__c);
    }

    @IsTest
    static void nabdRaisesItsOwnPriorityAndSummaryButNeverLowersIt() {
        Id caseId = createCase('op-create-0030', 'nabd-issue-test-0030');
        Map<String, Object> command = change('update_case', 'op-update-0030', caseId);
        command.put('priority', 'High');
        command.put('summary', 'Customer now also reports a duplicate charge.');
        post(command);
        Case current = reload(caseId);
        System.assertEquals('High', current.Priority);
        System.assertEquals('High', current.NABD_Assessed_Priority__c);
        System.assertEquals('Customer now also reports a duplicate charge.', current.English_Summary__c);
        command = change('update_case', 'op-update-0031', caseId);
        command.put('priority', 'Low');
        post(command);
        System.assertEquals('High', reload(caseId).Priority);
    }

    @IsTest
    static void reopeningFollowsTheLifecycle() {
        Id caseId = createCase('op-create-0040', 'nabd-issue-test-0040');
        System.assertEquals('case_open', post(change('reopen_case', 'op-reopen-0040', caseId)).get('reason'));
        Case closed = new Case(Id = caseId, Status = 'Closed', Resolution_Outcome__c = 'resolved');
        update as system closed;
        System.assertEquals('case_closed', post(change('update_case', 'op-update-0041', caseId)).get('reason'));
        Map<String, Object> result = post(change('reopen_case', 'op-reopen-0042', caseId));
        System.assertEquals('confirmed', result.get('status'));
        Case reopened = reload(caseId);
        System.assertEquals('Working', reopened.Status);
        System.assertEquals(1, reopened.Reopen_Count__c.intValue());
        System.assertEquals(null, reopened.Resolution_Outcome__c);

        Case duplicate = new Case(Id = caseId, Status = 'Closed', Resolution_Outcome__c = 'duplicate');
        update as system duplicate;
        System.assertEquals('closed_as_duplicate', post(change('reopen_case', 'op-reopen-0043', caseId)).get('reason'));
    }

    @IsTest
    static void closingNeedsConfirmationEvidenceAndCompletedStaffAction() {
        Map<String, Object> creationCommand = creation('op-create-0050', 'nabd-issue-test-0050');
        creationCommand.put('staffAction', 'refund');
        creationCommand.put('category', 'Returns_Refunds');
        Id caseId = (Id) (String) post(creationCommand).get('caseId');
        System.assertEquals('Returns_Refunds', [SELECT Complaint_Category__c FROM Case WHERE Id = :caseId WITH SYSTEM_MODE].Complaint_Category__c);

        Map<String, Object> request = change('request_closure', 'op-request-0050', caseId);
        request.put('evidence', new List<Object>{evidence('fact:0050', 'verified_fact', 'Refund recorded.')});
        System.assertEquals('staff_action_pending', post(request).get('reason'));

        Map<String, Object> noConfirmation = change('close_case', 'op-close-0050', caseId);
        noConfirmation.put('confirmationMessageId', 'msg-0050');
        noConfirmation.put('evidence', new List<Object>{evidence('fact:0050', 'verified_fact', 'Refund recorded.')});
        Map<String, Object> refused = post(noConfirmation);
        System.assertEquals('invalid_command', refused.get('reason'));
        System.assertEquals(0, receipts('op-close-0050'));

        Map<String, Object> close = change('close_case', 'op-close-0051', caseId);
        close.put('confirmationMessageId', 'msg-0051');
        close.put('sentiment', 'positive');
        close.put('evidence', new List<Object>{
            evidence('msg-0051', 'customer_confirmation', 'The refund arrived, thank you, all sorted.'),
            evidence('fact:0051', 'verified_fact', 'Refund recorded.')
        });
        System.assertEquals('staff_action_pending', post(close).get('reason'));

        Case completed = new Case(Id = caseId, Staff_Action_Completed__c = true);
        update as system completed;
        close.put('operationId', 'op-close-0052');
        close.put('expectedVersion', NabdCaseCommands.version([SELECT SystemModstamp FROM Case WHERE Id = :caseId WITH SYSTEM_MODE].SystemModstamp));
        Map<String, Object> result = post(close);
        System.assertEquals('confirmed', result.get('status'), String.valueOf(result));
        Case current = reload(caseId);
        System.assertEquals('Closed', current.Status);
        System.assertEquals('resolved', current.Resolution_Outcome__c);
        System.assertEquals('msg-0051', current.Resolution_Confirmation_Message__c);
    }

    @IsTest
    static void aClosureRequestWaitsForTheCustomer() {
        Id caseId = createCase('op-create-0060', 'nabd-issue-test-0060');
        Map<String, Object> request = change('request_closure', 'op-request-0060', caseId);
        request.put('evidence', new List<Object>{evidence('fact:0060', 'verified_fact', 'Delivered on 16 November.')});
        System.assertEquals('confirmed', post(request).get('status'));
        System.assertEquals('Waiting on Customer', reload(caseId).Status);
    }

    @IsTest
    static void evidenceIsAddedOnlyOnce() {
        Id caseId = createCase('op-create-0070', 'nabd-issue-test-0070');
        System.assertEquals(1, post(change('update_case', 'op-update-0070', caseId)).get('evidenceAdded'));
        System.assertEquals(0, post(change('update_case', 'op-update-0071', caseId)).get('evidenceAdded'));
        System.assertEquals(3, [SELECT COUNT() FROM Complaint_Evidence__c WHERE Case__c = :caseId WITH SYSTEM_MODE]);
    }

    @IsTest
    static void lowConfidenceCasesGoToGeneralSupportForReview() {
        Map<String, Object> command = creation('op-create-0080', 'nabd-issue-test-0080');
        command.put('category', 'General_Support');
        command.put('needsReview', true);
        command.put('confidence', 0.4);
        command.remove('orderNumber');
        Map<String, Object> result = post(command);
        System.assertEquals('NABD_General_Support', result.get('queue'));
        System.assertEquals(true, reload((Id) (String) result.get('caseId')).Needs_Review__c);
    }

    static Integer streak(Id caseId) {
        Decimal value = [SELECT Negative_Streak__c FROM Case WHERE Id = :caseId WITH SYSTEM_MODE].Negative_Streak__c;
        return value == null ? null : value.intValue();
    }

    @IsTest
    static void eachNewCustomerContactRecordsItsSentimentAndTheNegativeStreak() {
        Id caseId = createCase('op-create-0100', 'nabd-issue-test-0100'); // negative report: streak 1
        System.assertEquals(1, streak(caseId));
        Map<String, String> sentiments = new Map<String, String>();
        for (Complaint_Evidence__c row : [SELECT Source_Message_Id__c, Assessed_Sentiment__c FROM Complaint_Evidence__c
                                          WHERE Case__c = :caseId WITH SYSTEM_MODE]) {
            sentiments.put(row.Source_Message_Id__c, row.Assessed_Sentiment__c);
        }
        System.assertEquals('negative', sentiments.get('msg-0001'));
        System.assertEquals(null, sentiments.get('fact:0001')); // facts are not customer contacts

        Map<String, Object> followUp = change('update_case', 'op-update-0100', caseId);
        followUp.put('sentiment', 'very_negative');
        String body = JSON.serialize(followUp);
        System.assertEquals(1, post(body).get('evidenceAdded'));
        System.assertEquals(2, streak(caseId));
        System.assertEquals(true, post(body).get('replayed'));
        System.assertEquals(2, streak(caseId)); // a replay is not another contact

        Map<String, Object> sameMessages = change('update_case', 'op-update-0101', caseId);
        sameMessages.put('sentiment', 'negative');
        System.assertEquals(0, post(sameMessages).get('evidenceAdded'));
        System.assertEquals(2, streak(caseId)); // no new customer evidence: no new contact

        Map<String, Object> calmer = change('update_case', 'op-update-0102', caseId);
        calmer.put('sentiment', 'neutral');
        calmer.put('evidence', new List<Object>{evidence('msg-0003', 'customer_report', 'Thanks, that helps.')});
        post(calmer);
        System.assertEquals(0, streak(caseId));
        System.assertEquals('neutral', [SELECT Assessed_Sentiment__c FROM Complaint_Evidence__c
                                        WHERE Case__c = :caseId AND Source_Message_Id__c = 'msg-0003'
                                        WITH SYSTEM_MODE].Assessed_Sentiment__c);

        Map<String, Object> worse = change('update_case', 'op-update-0103', caseId);
        worse.put('sentiment', 'negative');
        worse.put('evidence', new List<Object>{evidence('msg-0004', 'customer_report', 'It broke again.')});
        post(worse);
        System.assertEquals(1, streak(caseId));
        Case closed = new Case(Id = caseId, Status = 'Closed', Resolution_Outcome__c = 'resolved');
        update as system closed;
        Map<String, Object> reopen = change('reopen_case', 'op-reopen-0104', caseId);
        reopen.put('sentiment', 'negative');
        reopen.put('evidence', new List<Object>{evidence('msg-0005', 'customer_report', 'Same problem again.')});
        System.assertEquals('confirmed', post(reopen).get('status'));
        System.assertEquals(1, streak(caseId)); // a new unresolved period starts a new streak
    }

    @IsTest
    static void malformedAndInvalidCommandsAreRefusedWithoutReceipts() {
        System.assertEquals('malformed', post('not json').get('reason'));
        System.assertEquals('malformed', post('{"operationId": "op-x-0001", "unexpected": 1}').get('reason'));
        Map<String, Object> command = creation('op-invalid-0001', 'nabd-issue-test-0090');
        command.put('operation', 'delete_case');
        System.assertEquals('invalid_command', post(command).get('reason'));
        command = creation('op-invalid-0002', 'nabd-issue-test-0091');
        command.put('category', 'Unknown');
        System.assertEquals('invalid_command', post(command).get('reason'));
        command = creation('op-invalid-0003', 'nabd-issue-test-0092');
        command.put('evidence', new List<Object>{evidence('fact:1', 'verified_fact', 'Only a fact.')});
        System.assertEquals('invalid_command', post(command).get('reason'));
        System.assertEquals(0, [SELECT COUNT() FROM NABD_Operation_Receipt__c WITH SYSTEM_MODE]);
    }
}
