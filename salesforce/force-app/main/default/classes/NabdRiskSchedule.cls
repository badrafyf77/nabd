/**
 * The hourly risk sweep (Phase 6): ages warnings such as "High priority
 * unresolved for 24 hours" that no record change would trigger.
 *
 * `python -m nabd salesforce configure` calls ensure(); `deploy` aborts the
 * job before deploying and calls ensure() again afterwards, because Salesforce
 * refuses to redeploy Apex that a scheduled job references.
 */
public without sharing class NabdRiskSchedule implements Schedulable {
    public static final String JOB_NAME = 'NABD risk evaluation (hourly)';
    public static final String CRON = '0 5 * * * ?'; // minute 5 of every hour

    public void execute(SchedulableContext context) {
        NabdRiskEvaluation.enqueueSweep();
    }

    /** Schedules the hourly job unless it already exists; returns its CronTrigger ID. */
    public static Id ensure() {
        List<CronTrigger> jobs = [
            SELECT Id FROM CronTrigger WHERE CronJobDetail.Name = :JOB_NAME WITH SYSTEM_MODE LIMIT 1
        ];
        return jobs.isEmpty() ? System.schedule(JOB_NAME, CRON, new NabdRiskSchedule()) : jobs[0].Id;
    }
}
