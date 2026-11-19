/**
 * Evaluates the customers named by NABD_Risk_Check__e events (Phase 6). Runs as
 * the Automated Process user, never as the integration user that changed the
 * case. See NabdRiskEvaluation.onRiskChecks.
 */
trigger NabdRiskCheckTrigger on NABD_Risk_Check__e (after insert) {
    NabdRiskEvaluation.onRiskChecks(Trigger.new);
}
