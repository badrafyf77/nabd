trigger KnowledgeEntryTrigger on KnowledgeEntry__c (before insert, before update, after insert, after update) {
    if (Trigger.isBefore) {
        KnowledgeEntryRevisions.prepare(Trigger.new, Trigger.oldMap);
    } else {
        KnowledgeEntryRevisions.retireSupersededRevisions(Trigger.new, Trigger.oldMap);
    }
}
