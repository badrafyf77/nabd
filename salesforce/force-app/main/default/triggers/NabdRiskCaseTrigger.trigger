/**
 * Publishes a risk check (NABD_Risk_Check__e, after commit) for each customer
 * whose NABD cases changed in a way the risk rules read (Phase 6). See
 * NabdRiskEvaluation.onCaseChange.
 */
trigger NabdRiskCaseTrigger on Case (after insert, after update, after delete, after undelete) {
    if (Trigger.isUpdate) {
        NabdRiskEvaluation.onCaseChange(Trigger.new, Trigger.oldMap);
    } else if (Trigger.isDelete) {
        NabdRiskEvaluation.onCaseChange(Trigger.old, null);
    } else {
        NabdRiskEvaluation.onCaseChange(Trigger.new, null);
    }
}
