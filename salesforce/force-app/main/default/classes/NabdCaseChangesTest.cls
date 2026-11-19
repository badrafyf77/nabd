/**
 * Which case and comment changes announce NABD_Case_Change__e: only staff changes
 * a customer could see, on NABD cases, never NABD's own gateway writes, and never
 * private notes. Events carry identifiers only.
 */
@IsTest
private class NabdCaseChangesTest {
    @TestSetup
    static void data() {
        Account account = new Account(Name = 'Change Test', ShopEasy_Customer_Id__c = 'C951');
        insert as system account;
        List<Case> cases = new List<Case>{
            new Case(AccountId = account.Id, Subject = 'NABD case', Status = 'Working', Origin = 'NABD Chat',
                     NABD_Issue_Id__c = 'issue-change-0001'),
            new Case(AccountId = account.Id, Subject = 'Other case', Status = 'Working', Origin = 'Email')
        };
        insert as system cases;
        NabdCaseChanges.published.clear();
    }

    static Case nabdCase() {
        return [SELECT Id, Status, Sentiment__c FROM Case WHERE NABD_Issue_Id__c = 'issue-change-0001'];
    }

    static Case otherCase() {
        return [SELECT Id, Status FROM Case WHERE NABD_Issue_Id__c = null AND Subject = 'Other case'];
    }

    @IsTest
    static void staffStatusChangeAnnouncesTheCaseOnly() {
        Case target = nabdCase();
        target.Status = 'Waiting on Customer';
        Test.startTest();
        update as system target;
        Test.stopTest();
        System.assertEquals(1, NabdCaseChanges.published.size());
        NABD_Case_Change__e event = NabdCaseChanges.published[0];
        System.assertEquals(target.Id, event.Case_Id__c);
        System.assertEquals('case', event.Change__c);
        System.assertEquals(null, event.Comment_Id__c);
    }

    @IsTest
    static void internalFieldChangesAndNonNabdCasesAnnounceNothing() {
        Case target = nabdCase();
        target.Sentiment__c = 'negative';
        Case other = otherCase();
        other.Status = 'Closed';
        update as system new List<Case>{target, other};
        System.assertEquals(0, NabdCaseChanges.published.size());
    }

    @IsTest
    static void staffActionCompletionIsVisible() {
        Case target = nabdCase();
        target.Staff_Action_Required__c = 'refund';
        update as system target;
        target.Staff_Action_Completed__c = true;
        update as system target;
        System.assertEquals(2, NabdCaseChanges.published.size());
    }

    @IsTest
    static void publicCommentsAnnounceAndPrivateNotesDoNot() {
        Case target = nabdCase();
        CaseComment reply = new CaseComment(ParentId = target.Id, CommentBody = 'We have refunded you.', IsPublished = true);
        CaseComment note = new CaseComment(ParentId = target.Id, CommentBody = 'Customer seems upset.', IsPublished = false);
        insert as system new List<CaseComment>{reply, note};
        System.assertEquals(1, NabdCaseChanges.published.size());
        System.assertEquals(reply.Id, NabdCaseChanges.published[0].Comment_Id__c);
        System.assertEquals(target.Id, NabdCaseChanges.published[0].Case_Id__c);

        note.CommentBody = 'Still a private note.';
        update as system note;
        System.assertEquals(1, NabdCaseChanges.published.size(), 'editing a private note announces nothing');

        reply.IsPublished = false; // a privacy change: the chat must withdraw the reply
        update as system reply;
        System.assertEquals(2, NabdCaseChanges.published.size());

        note.IsPublished = true;
        update as system note;
        System.assertEquals(3, NabdCaseChanges.published.size());

        delete as system note;
        System.assertEquals(4, NabdCaseChanges.published.size());
        delete as system reply; // already private again: nothing to withdraw
        System.assertEquals(4, NabdCaseChanges.published.size());
    }

    @IsTest
    static void commentsOnOtherCasesAnnounceNothing() {
        insert as system new CaseComment(ParentId = otherCase().Id, CommentBody = 'Hello', IsPublished = true);
        System.assertEquals(0, NabdCaseChanges.published.size());
    }

    @IsTest
    static void deletionAndRestoreAreAnnounced() {
        Case target = nabdCase();
        delete as system target;
        undelete target;
        System.assertEquals(2, NabdCaseChanges.published.size());
        System.assertEquals('case_deleted', NabdCaseChanges.published[0].Change__c);
        System.assertEquals('case_restored', NabdCaseChanges.published[1].Change__c);
    }

    @IsTest
    static void suppressedTransactionsAnnounceNothing() {
        Case target = nabdCase();
        NabdCaseChanges.suppressed = true;
        target.Status = 'Closed';
        update as system target;
        insert as system new CaseComment(ParentId = target.Id, CommentBody = 'x', IsPublished = true);
        NabdCaseChanges.suppressed = false;
        System.assertEquals(0, NabdCaseChanges.published.size());
    }

    @IsTest
    static void gatewayWritesDoNotEcho() {
        Account account = [SELECT Id FROM Account WHERE ShopEasy_Customer_Id__c = 'C951'];
        Case target = [SELECT Id, SystemModstamp FROM Case WHERE NABD_Issue_Id__c = 'issue-change-0001'];
        target.Status = 'Waiting on Customer';
        update as system target;
        NabdCaseChanges.published.clear();
        target = [SELECT Id, SystemModstamp FROM Case WHERE Id = :target.Id];
        Map<String, Object> command = new Map<String, Object>{
            'operationId' => 'op-change-test-0001', 'operation' => 'update_case', 'customerId' => 'C951',
            'caseId' => target.Id, 'expectedVersion' => NabdCaseCommands.version(target.SystemModstamp),
            'evidence' => new List<Object>{new Map<String, Object>{
                'ref' => 'msg-change-0001', 'kind' => 'customer_report', 'text' => 'More details.'}}
        };
        RestRequest request = new RestRequest();
        request.requestURI = '/services/apexrest/nabd/v1/case-commands';
        request.httpMethod = 'POST';
        request.requestBody = Blob.valueOf(JSON.serialize(command));
        RestContext.request = request;
        RestContext.response = new RestResponse();
        NabdCaseCommands.post();
        Map<String, Object> result = (Map<String, Object>) JSON.deserializeUntyped(RestContext.response.responseBody.toString());
        System.assertEquals('confirmed', result.get('status'));
        System.assertEquals('Working', [SELECT Status FROM Case WHERE Id = :target.Id].Status);
        System.assertEquals(0, NabdCaseChanges.published.size(), 'the status change was NABD\'s own');
        System.assertEquals(false, NabdCaseChanges.suppressed, 'suppression ends with the command');
    }
}
