trigger NabdCaseCommentTrigger on CaseComment (after insert, after update, after delete) {
    if (Trigger.isUpdate) {
        NabdCaseChanges.comments(Trigger.new, Trigger.oldMap, 'comment');
    } else if (Trigger.isDelete) {
        NabdCaseChanges.comments(Trigger.old, null, 'comment');
    } else {
        NabdCaseChanges.comments(Trigger.new, null, 'comment');
    }
}
