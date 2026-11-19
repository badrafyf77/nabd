/**
 * Before save: derives Rule_Key__c (code@version) and refuses unknown codes or
 * invalid parameters. After a definition change: queues a sweep (Phase 6).
 */
trigger NabdRiskRuleTrigger on Risk_Rule__c (before insert, before update, after insert, after update,
                                             after delete, after undelete) {
    if (Trigger.isBefore) {
        NabdRiskEvaluation.prepareRules(Trigger.new);
    } else if (Trigger.isUpdate) {
        NabdRiskEvaluation.onRuleChange(Trigger.new, Trigger.oldMap);
    } else if (Trigger.isDelete) {
        NabdRiskEvaluation.onRuleChange(Trigger.old, null);
    } else {
        NabdRiskEvaluation.onRuleChange(Trigger.new, null);
    }
}
