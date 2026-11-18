trigger NabdCaseChangeTrigger on Case (after update, after delete, after undelete) {
    if (Trigger.isUpdate) {
        NabdCaseChanges.cases(Trigger.new, Trigger.oldMap, 'case');
    } else if (Trigger.isDelete) {
        NabdCaseChanges.cases(Trigger.old, null, 'case_deleted');
    } else {
        NabdCaseChanges.cases(Trigger.new, null, 'case_restored');
    }
}
