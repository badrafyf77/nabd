/**
 * DML runs in system mode so the tests check revision rules, not the running
 * user's field permissions (those are covered by the NABD_Staff permission set).
 */
@IsTest
private class KnowledgeEntryRevisionsTest {
    private static KnowledgeEntry__c entry(String key, Integer revision, String status) {
        return new KnowledgeEntry__c(
            Name = key, Entry_Key__c = key, Revision__c = revision, Entry_Type__c = 'faq',
            Status__c = status, Language__c = 'en', Body__c = 'Body ' + revision
        );
    }

    @IsTest
    static void revisionKeyAndPublishTimeAreSet() {
        KnowledgeEntry__c published = entry('faq.test', 1, 'Published');
        insert as system published;
        published = [SELECT Revision_Key__c, Published_At__c FROM KnowledgeEntry__c WHERE Id = :published.Id WITH SYSTEM_MODE];
        System.assertEquals('faq.test@1', published.Revision_Key__c);
        System.assertNotEquals(null, published.Published_At__c);
    }

    @IsTest
    static void publishingANewRevisionRetiresThePreviousOne() {
        KnowledgeEntry__c first = entry('faq.test', 1, 'Published');
        insert as system first;
        KnowledgeEntry__c second = entry('faq.test', 2, 'Draft');
        insert as system second;
        second.Status__c = 'Published';
        update as system second;

        first = [SELECT Status__c, Retired_At__c FROM KnowledgeEntry__c WHERE Id = :first.Id WITH SYSTEM_MODE];
        System.assertEquals('Retired', first.Status__c);
        System.assertNotEquals(null, first.Retired_At__c);
        System.assertEquals(1, [SELECT COUNT() FROM KnowledgeEntry__c WHERE Entry_Key__c = 'faq.test' AND Status__c = 'Published' WITH SYSTEM_MODE]);
    }

    @IsTest
    static void twoRevisionsCannotBePublishedTogether() {
        List<KnowledgeEntry__c> both = new List<KnowledgeEntry__c>{
            entry('faq.test', 1, 'Published'), entry('faq.test', 2, 'Published')
        };
        Database.SaveResult[] results = Database.insert(both, false, AccessLevel.SYSTEM_MODE);
        Integer failures = 0;
        for (Database.SaveResult result : results) {
            if (!result.isSuccess()) {
                failures++;
            }
        }
        System.assertEquals(1, failures);
        System.assertEquals(1, [SELECT COUNT() FROM KnowledgeEntry__c WHERE Status__c = 'Published' WITH SYSTEM_MODE]);
    }

    @IsTest
    static void otherEntriesAreUntouched() {
        insert as system entry('faq.other', 1, 'Published');
        insert as system entry('faq.test', 1, 'Published');
        System.assertEquals(2, [SELECT COUNT() FROM KnowledgeEntry__c WHERE Status__c = 'Published' WITH SYSTEM_MODE]);
    }
}
