/**
 * Keeps KnowledgeEntry__c revisions consistent while staff edit them.
 *
 * - Revision_Key__c is always Entry_Key__c + '@' + Revision__c, so staff only
 *   choose the entry key and revision number.
 * - Publishing a revision retires the previously published revision of the
 *   same entry, so customers only ever receive one current revision.
 *
 * Runs in system mode without sharing: the "one published revision" rule must
 * hold even for revisions the editing user cannot see or edit.
 */
public without sharing class KnowledgeEntryRevisions {
    public static final String PUBLISHED = 'Published';
    public static final String RETIRED = 'Retired';

    public static void prepare(List<KnowledgeEntry__c> entries, Map<Id, KnowledgeEntry__c> oldById) {
        for (KnowledgeEntry__c entry : entries) {
            if (String.isNotBlank(entry.Entry_Key__c) && entry.Revision__c != null) {
                entry.Revision_Key__c = entry.Entry_Key__c + '@' + String.valueOf(entry.Revision__c.intValue());
            }
            KnowledgeEntry__c old = oldById == null ? null : oldById.get(entry.Id);
            if (changedTo(entry, old, PUBLISHED) && entry.Published_At__c == null) {
                entry.Published_At__c = System.now();
            }
            if (changedTo(entry, old, RETIRED) && entry.Retired_At__c == null) {
                entry.Retired_At__c = System.now();
            }
        }
    }

    public static void retireSupersededRevisions(List<KnowledgeEntry__c> entries, Map<Id, KnowledgeEntry__c> oldById) {
        Map<String, Id> publishedByKey = new Map<String, Id>();
        for (KnowledgeEntry__c entry : entries) {
            KnowledgeEntry__c old = oldById == null ? null : oldById.get(entry.Id);
            if (!changedTo(entry, old, PUBLISHED)) {
                continue;
            }
            if (publishedByKey.containsKey(entry.Entry_Key__c)) {
                entry.addError('Publish only one revision of ' + entry.Entry_Key__c + ' at a time.');
                continue;
            }
            publishedByKey.put(entry.Entry_Key__c, entry.Id);
        }
        if (publishedByKey.isEmpty()) {
            return;
        }
        List<KnowledgeEntry__c> superseded = [
            SELECT Id FROM KnowledgeEntry__c
            WHERE Entry_Key__c IN :publishedByKey.keySet()
              AND Status__c = :PUBLISHED
              AND Id NOT IN :publishedByKey.values()
            WITH SYSTEM_MODE
        ];
        for (KnowledgeEntry__c entry : superseded) {
            entry.Status__c = RETIRED;
        }
        update as system superseded;
    }

    private static Boolean changedTo(KnowledgeEntry__c entry, KnowledgeEntry__c old, String status) {
        return entry.Status__c == status && (old == null || old.Status__c != status);
    }
}
