/**
 * NABD's typed case command gateway: the only way the NABD backend changes cases.
 *
 * POST /services/apexrest/nabd/v1/case-commands runs one command:
 * create_case, update_case, reopen_case, request_closure or close_case.
 * GET  /services/apexrest/nabd/v1/case-commands/{operationId}?customerId=C001
 * returns the stored receipt of an earlier command, or status "unknown".
 *
 * Each command is one transaction: find the receipt of its operation ID (same
 * request body: return the stored result; different body: refuse), check the
 * customer owns the case or order, lock the customer's account and the case,
 * compare the case version NABD read, check the lifecycle transition, apply
 * it, add evidence that is not there yet, and save a receipt. The unique
 * Operation_Id__c makes a concurrent duplicate fail and roll back, after which
 * the stored receipt is returned.
 *
 * Staff stay in charge: category and owner are set only on create; priority
 * changes only while it still equals NABD's last assessment; the English
 * summary is replaced only while it is still the text NABD wrote; a case with
 * a required staff action cannot be closed until staff mark it completed.
 *
 * Runs in system mode without sharing because the integration user has no
 * object create/edit permission: these rules are its only write path. Its
 * transactions publish no NABD_Case_Change__e: NABD already knows what it wrote.
 *
 * Phase 6: a command that adds new customer evidence (a report or confirmation)
 * is one customer contact. Its sentiment is stamped on that new evidence and
 * kept as the case's negative-contact streak, which the risk engine reads.
 * Replays and commands without new customer evidence change neither.
 */
@RestResource(urlMapping='/nabd/v1/case-commands/*')
global without sharing class NabdCaseCommands {
    public static final String CREATE = 'create_case';
    public static final String UPDATE_OP = 'update_case';
    public static final String REOPEN = 'reopen_case';
    public static final String REQUEST_CLOSURE = 'request_closure';
    public static final String CLOSE = 'close_case';

    public static final String REVISE_SUMMARY = 'revise_summary';

    static final Set<String> OPERATIONS = new Set<String>{CREATE, UPDATE_OP, REOPEN, REQUEST_CLOSURE, CLOSE, REVISE_SUMMARY};
    static final Set<String> CATEGORIES = new Set<String>{
        'Delivery', 'Orders_Products', 'Billing_Payments', 'Returns_Refunds', 'General_Support'
    };
    static final Map<String, Integer> PRIORITY_RANK = new Map<String, Integer>{'Low' => 1, 'Medium' => 2, 'High' => 3};
    static final Set<String> SENTIMENTS = new Set<String>{'positive', 'neutral', 'negative', 'very_negative'};
    static final Set<String> URGENCIES = new Set<String>{'low', 'medium', 'high'};
    static final Set<String> LANGUAGES = new Set<String>{'en', 'fr', 'ar', 'ary', 'mixed'};
    static final Set<String> SCRIPTS = new Set<String>{'latin', 'arabic', 'mixed'};
    static final Set<String> STAFF_ACTIONS = new Set<String>{
        'refund', 'replacement', 'billing_review', 'carrier_investigation', 'other'
    };
    static final Set<String> EVIDENCE_KINDS = new Set<String>{'customer_report', 'verified_fact', 'customer_confirmation'};
    static final Set<String> CONTACT_KINDS = new Set<String>{'customer_report', 'customer_confirmation'};
    static final Set<String> NEGATIVE = new Set<String>{'negative', 'very_negative'};
    static final Pattern OPERATION_ID = Pattern.compile('[A-Za-z0-9_.:\\-]{8,64}');
    static final Pattern ISSUE_ID = Pattern.compile('[A-Za-z0-9_.:\\-]{8,64}');
    static final Pattern CUSTOMER_ID = Pattern.compile('C\\d{3,6}');
    static final Pattern ORDER_NUMBER = Pattern.compile('SE-\\d{4,6}');
    static final Pattern REF = Pattern.compile('[A-Za-z0-9_.:\\-]{1,70}');
    static final Integer MAX_EVIDENCE = 20;
    static final Integer MAX_TEXT = 32000;

    public class Command {
        public String operationId;
        public String operation;
        public String customerId;
        public String conversationId;
        public String caseId;
        public String expectedVersion;
        public String issueId;
        public String orderNumber;
        public String category;
        public String subject;
        public String summary;
        public String language;
        public String sentiment;
        public String urgency;
        public Decimal confidence;
        public Boolean needsReview;
        public String priority;
        public String priorityReason;
        public String staffAction;
        public String confirmationMessageId;
        public List<EvidenceItem> evidence;
    }

    public class EvidenceItem {
        public String ref; // source message ID or fact reference, unique within the case
        public String kind;
        public String text;
        public String language;
        public String script;
        public String verifiedSource;
        public String capturedAt;
    }

    public class Result {
        public String status; // confirmed, conflict, failed (or unknown for a receipt lookup)
        public String reason;
        public String message;
        public String operationId;
        public String operation;
        public String customerId;
        public Boolean recorded = false;
        public Boolean replayed = false;
        public String caseId;
        public String caseNumber;
        public String caseVersion;
        public String caseStatus;
        public String queue;
        public List<String> overrides = new List<String>();
        public Integer evidenceAdded = 0;
    }

    // --- HTTP ---------------------------------------------------------------------

    @HttpPost
    global static void post() {
        Blob body = RestContext.request.requestBody;
        Command command;
        try {
            command = (Command) JSON.deserializeStrict(body == null ? '' : body.toString(), Command.class);
        } catch (Exception error) {
            respond(refused(null, 'malformed', 'The request is not a valid case command.'));
            return;
        }
        String problem = problemWith(command);
        if (problem != null) {
            respond(refused(command, 'invalid_command', problem));
            return;
        }
        String fingerprint = EncodingUtil.convertToHex(Crypto.generateDigest('SHA-256', body));
        respond(execute(command, fingerprint));
    }

    @HttpGet
    global static void get() {
        RestRequest request = RestContext.request;
        String operationId = request.requestURI.substringAfterLast('/');
        String customerId = request.params.get('customerId');
        NABD_Operation_Receipt__c receipt = matches(OPERATION_ID, operationId) ? findReceipt(operationId) : null;
        Result result = receipt == null ? null : stored(receipt);
        if (result == null || customerId == null || !customerId.equals(result.customerId)) {
            result = new Result();
            result.status = 'unknown';
            result.operationId = operationId;
        } else {
            result.replayed = true;
        }
        respond(result);
    }

    static void respond(Result result) {
        RestResponse response = RestContext.response;
        response.statusCode = 200;
        response.addHeader('Content-Type', 'application/json');
        response.responseBody = Blob.valueOf(JSON.serialize(result));
    }

    // --- Execution ------------------------------------------------------------------

    static Result execute(Command command, String fingerprint) {
        // NABD's own case writes must not come back to the chat as staff changes (NabdCaseChanges).
        Boolean previous = NabdCaseChanges.suppressed;
        NabdCaseChanges.suppressed = true;
        try {
            return run(command, fingerprint);
        } finally {
            NabdCaseChanges.suppressed = previous;
        }
    }

    static Result run(Command command, String fingerprint) {
        NABD_Operation_Receipt__c existing = findReceipt(command.operationId);
        if (existing != null) {
            return replay(command, existing, fingerprint);
        }
        Savepoint savepoint = Database.setSavepoint();
        Result result;
        try {
            result = apply(command);
            result.recorded = true;
            NABD_Operation_Receipt__c saved = receipt(command, fingerprint, result);
            insert as system saved;
        } catch (DmlException error) {
            Database.rollback(savepoint);
            if (error.getDmlType(0) == StatusCode.DUPLICATE_VALUE) {
                existing = findReceipt(command.operationId);
                if (existing != null) {
                    return replay(command, existing, fingerprint); // a concurrent duplicate committed first
                }
            }
            result = outcome(command, 'failed', 'salesforce_rejected',
                             'Salesforce rejected the change (' + String.valueOf(error.getDmlType(0)) + ').');
            result.recorded = true;
            NABD_Operation_Receipt__c saved = receipt(command, fingerprint, result);
            insert as system saved;
        }
        return result;
    }

    static Result replay(Command command, NABD_Operation_Receipt__c receipt, String fingerprint) {
        if (!fingerprint.equals(receipt.Request_Fingerprint__c)) {
            return refused(command, 'operation_reused', 'This operation ID was already used for a different request.');
        }
        Result result = stored(receipt);
        result.replayed = true;
        return result;
    }

    static Result apply(Command command) {
        // Every query states WITH SYSTEM_MODE: the integration user itself cannot read evidence or
        // receipts, and an implicit-mode query on them fails at runtime.
        String customerId = command.customerId;
        Id caseId = String.isBlank(command.caseId) ? null : Id.valueOf(command.caseId);
        List<Account> accounts = [
            SELECT Id FROM Account WHERE ShopEasy_Customer_Id__c = :customerId WITH SYSTEM_MODE LIMIT 1 FOR UPDATE
        ];
        if (accounts.isEmpty()) {
            return outcome(command, 'failed', 'unknown_customer', 'The customer is not in Salesforce.');
        }
        Account account = accounts[0];
        if (command.operation == CREATE) {
            return createCase(command, account);
        }
        List<Case> cases = [
            SELECT Id, AccountId, CaseNumber, Status, IsClosed, Priority, SystemModstamp, OwnerId,
                   NABD_Issue_Id__c, English_Summary__c, NABD_Summary_Hash__c, NABD_Assessed_Priority__c,
                   Reopen_Count__c, Resolution_Outcome__c, Staff_Action_Required__c, Staff_Action_Completed__c,
                   Negative_Streak__c
            FROM Case WHERE Id = :caseId WITH SYSTEM_MODE LIMIT 1 FOR UPDATE
        ];
        if (cases.isEmpty() || cases[0].AccountId != account.Id) {
            return outcome(command, 'failed', 'case_not_found', 'The case is not on this customer\'s account.');
        }
        Case target = cases[0];
        if (!sameVersion(target.SystemModstamp, command.expectedVersion)) {
            return conflict(command, target, 'version_changed', 'The case changed after NABD read it.');
        }
        if (command.operation == REVISE_SUMMARY) {
            return reviseSummary(command, target);
        }
        if (command.operation == REOPEN) {
            return reopenCase(command, target);
        }
        if (target.IsClosed) {
            return conflict(command, target, 'case_closed', 'The case is closed.');
        }
        if (command.operation == UPDATE_OP) {
            return updateCase(command, target);
        }
        if (staffActionPending(target)) {
            return conflict(command, target, 'staff_action_pending', 'Staff still have to complete the required action.');
        }
        return command.operation == CLOSE ? closeCase(command, target) : requestClosure(command, target);
    }

    static Result createCase(Command command, Account account) {
        String issueId = command.issueId;
        String orderNumber = command.orderNumber;
        String customerId = command.customerId;
        Id accountId = account.Id;
        List<Case> sameIssue = [
            SELECT Id, AccountId, CaseNumber, Status, SystemModstamp, OwnerId FROM Case
            WHERE NABD_Issue_Id__c = :issueId WITH SYSTEM_MODE LIMIT 1
        ];
        if (!sameIssue.isEmpty()) {
            if (sameIssue[0].AccountId != account.Id) {
                return outcome(command, 'failed', 'issue_exists', 'This issue ID is already used.');
            }
            return conflict(command, sameIssue[0], 'issue_exists', 'A case already exists for this issue.');
        }
        Id orderId = null;
        if (String.isNotBlank(command.orderNumber)) {
            List<ShopEasy_Order__c> orders = [
                SELECT Id, Account__c FROM ShopEasy_Order__c WHERE Order_Number__c = :orderNumber WITH SYSTEM_MODE LIMIT 1
            ];
            if (orders.isEmpty() || orders[0].Account__c != account.Id) {
                return outcome(command, 'failed', 'order_not_found', 'The order is not on this customer\'s account.');
            }
            orderId = orders[0].Id;
        }
        Id queueId = queueFor(command.category);
        if (queueId == null) {
            return outcome(command, 'failed', 'queue_missing', 'No queue is configured for this category.');
        }
        List<Contact> contacts = [
            SELECT Id FROM Contact WHERE AccountId = :accountId AND ShopEasy_Customer_Id__c = :customerId WITH SYSTEM_MODE LIMIT 1
        ];
        Case created = new Case(
            AccountId = account.Id, ContactId = contacts.isEmpty() ? null : contacts[0].Id, OwnerId = queueId,
            Origin = 'NABD Chat', Status = 'New', Subject = command.subject.left(255),
            Description = firstText(command, 'customer_report'), Priority = command.priority,
            NABD_Assessed_Priority__c = command.priority, NABD_Priority_Reason__c = clip(command.priorityReason, 255),
            Complaint_Category__c = command.category, Customer_Language__c = command.language,
            Sentiment__c = command.sentiment, Urgency__c = command.urgency,
            Classification_Confidence__c = command.confidence, Needs_Review__c = command.needsReview == true,
            English_Summary__c = command.summary.left(MAX_TEXT), NABD_Summary_Hash__c = hashOf(command.summary.left(MAX_TEXT)),
            NABD_Issue_Id__c = command.issueId, NABD_Conversation_Id__c = clip(command.conversationId, 64),
            ShopEasy_Order__c = orderId, Staff_Action_Required__c = command.staffAction,
            Staff_Action_Completed__c = false, Negative_Streak__c = 0
        );
        observeContact(command, created, contactIn(command.evidence), true);
        insert as system created;
        Result result = outcome(command, 'confirmed', null, null);
        result.evidenceAdded = insertEvidence(command, newEvidence(command, created.Id, command.issueId));
        return describe(result, created.Id);
    }

    static Result reviseSummary(Command command, Case target) {
        if (String.isBlank(target.NABD_Issue_Id__c)) {
            return conflict(command, target, 'not_nabd_case', 'This case is not managed by NABD.');
        }
        if (String.isBlank(target.NABD_Summary_Hash__c) ||
            !target.NABD_Summary_Hash__c.equals(hashOf(target.English_Summary__c))) {
            return conflict(command, target, 'staff_override', 'Staff own the current summary.');
        }
        Case wording = new Case(Id = target.Id, English_Summary__c = command.summary,
                                NABD_Summary_Hash__c = hashOf(command.summary));
        update as system wording;
        return describe(outcome(command, 'confirmed', null, null), target.Id);
    }

    static Result updateCase(Command command, Case target) {
        Result result = outcome(command, 'confirmed', null, null);
        if (target.Status == 'Waiting on Customer') {
            target.Status = 'Working'; // the customer came back with more information
        }
        return save(command, target, result, false);
    }

    /** Applies the assessment, the contact and the new evidence of an update to an existing case. */
    static Result save(Command command, Case target, Result result, Boolean restartStreak) {
        List<Complaint_Evidence__c> fresh = newEvidence(command, target.Id, issuePrefix(target));
        observeContact(command, target, contactIn(fresh), restartStreak);
        applyAssessment(command, target, result);
        update as system target;
        result.evidenceAdded = insertEvidence(command, fresh);
        return describe(result, target.Id);
    }

    static Result reopenCase(Command command, Case target) {
        if (!target.IsClosed) {
            return conflict(command, target, 'case_open', 'The case is still open.');
        }
        if (target.Resolution_Outcome__c == 'duplicate') {
            return conflict(command, target, 'closed_as_duplicate', 'Staff closed this case as a duplicate.');
        }
        Result result = outcome(command, 'confirmed', null, null);
        target.Status = 'Working';
        target.Reopen_Count__c = (target.Reopen_Count__c == null ? 0 : target.Reopen_Count__c) + 1;
        target.Last_Reopened_At__c = System.now();
        target.Resolution_Outcome__c = null;
        target.Resolution_Confirmed_At__c = null;
        target.Resolution_Confirmation_Message__c = null;
        if (String.isNotBlank(target.Staff_Action_Required__c)) {
            target.Staff_Action_Completed__c = false;
        }
        return save(command, target, result, true); // a new unresolved period: the streak restarts
    }

    static Result requestClosure(Command command, Case target) {
        Result result = outcome(command, 'confirmed', null, null);
        target.Status = 'Waiting on Customer'; // resolved on the record; the customer is asked to confirm
        return save(command, target, result, false);
    }

    static Result closeCase(Command command, Case target) {
        Result result = outcome(command, 'confirmed', null, null);
        target.Status = 'Closed';
        target.Resolution_Outcome__c = 'resolved';
        target.Resolution_Confirmed_At__c = System.now();
        target.Resolution_Confirmation_Message__c = command.confirmationMessageId.left(64);
        return save(command, target, result, false);
    }

    /** NABD's own assessment fields, without overwriting what staff changed. */
    static void applyAssessment(Command command, Case target, Result result) {
        if (String.isNotBlank(command.sentiment)) {
            target.Sentiment__c = command.sentiment;
        }
        if (String.isNotBlank(command.urgency)) {
            target.Urgency__c = command.urgency;
        }
        if (String.isNotBlank(command.priority) && rank(command.priority) > rank(target.Priority)) {
            Boolean nabdOwned = target.NABD_Assessed_Priority__c != null
                && target.NABD_Assessed_Priority__c.equals(target.Priority);
            if (nabdOwned) {
                target.Priority = command.priority;
                target.NABD_Assessed_Priority__c = command.priority;
                target.NABD_Priority_Reason__c = clip(command.priorityReason, 255);
            } else {
                result.overrides.add('priority');
            }
        }
        if (String.isNotBlank(command.summary) && !command.summary.equals(target.English_Summary__c)) {
            String current = hashOf(target.English_Summary__c);
            Boolean nabdWritten = target.NABD_Summary_Hash__c != null && current != null
                && current.equals(target.NABD_Summary_Hash__c);
            if (nabdWritten) {
                target.English_Summary__c = command.summary.left(MAX_TEXT);
                target.NABD_Summary_Hash__c = hashOf(target.English_Summary__c);
            } else {
                result.overrides.add('summary');
            }
        }
        if (String.isNotBlank(command.staffAction) && String.isBlank(target.Staff_Action_Required__c)) {
            target.Staff_Action_Required__c = command.staffAction;
            target.Staff_Action_Completed__c = false;
        }
    }

    /**
     * One customer contact: the command carries sentiment and adds new customer evidence. The
     * streak counts consecutive negative contacts; any other contact resets it. A create or a
     * reopening starts a new unresolved period, so the streak restarts there.
     */
    static void observeContact(Command command, Case target, Boolean contact, Boolean restart) {
        if (!contact || String.isBlank(command.sentiment)) {
            return;
        }
        Decimal streak = restart || target.Negative_Streak__c == null ? 0 : target.Negative_Streak__c;
        target.Negative_Streak__c = NEGATIVE.contains(command.sentiment) ? streak + 1 : 0;
    }

    static Boolean contactIn(List<EvidenceItem> items) {
        for (EvidenceItem item : items == null ? new List<EvidenceItem>() : items) {
            if (CONTACT_KINDS.contains(item.kind)) {
                return true;
            }
        }
        return false;
    }

    static Boolean contactIn(List<Complaint_Evidence__c> rows) {
        for (Complaint_Evidence__c row : rows) {
            if (CONTACT_KINDS.contains(row.Evidence_Kind__c)) {
                return true;
            }
        }
        return false;
    }

    static Integer insertEvidence(Command command, List<Complaint_Evidence__c> fresh) {
        for (Complaint_Evidence__c row : fresh) {
            if (CONTACT_KINDS.contains(row.Evidence_Kind__c) && String.isNotBlank(command.sentiment)) {
                row.Assessed_Sentiment__c = command.sentiment; // NABD's assessment at this contact
            }
        }
        if (!fresh.isEmpty()) {
            insert as system fresh;
        }
        return fresh.size();
    }

    /** The command's evidence rows whose keys the case does not have yet (not inserted). */
    static List<Complaint_Evidence__c> newEvidence(Command command, Id caseId, String prefix) {
        if (command.evidence == null || command.evidence.isEmpty()) {
            return new List<Complaint_Evidence__c>();
        }
        Map<String, Complaint_Evidence__c> byKey = new Map<String, Complaint_Evidence__c>();
        for (EvidenceItem item : command.evidence) {
            String key = (prefix + ':' + item.ref).left(140);
            if (byKey.containsKey(key)) {
                continue;
            }
            byKey.put(key, new Complaint_Evidence__c(
                Case__c = caseId, Evidence_Key__c = key, Source_Message_Id__c = item.ref.left(64),
                Evidence_Kind__c = item.kind, Original_Text__c = item.text.left(MAX_TEXT),
                Original_Language__c = item.language, Original_Script__c = item.script,
                Verified_Source__c = clip(item.verifiedSource, 255), Captured_At__c = parseTime(item.capturedAt),
                NABD_Conversation_Id__c = clip(command.conversationId, 64)
            ));
        }
        Set<String> keys = new Set<String>(byKey.keySet());
        for (Complaint_Evidence__c row : [
            SELECT Evidence_Key__c FROM Complaint_Evidence__c WHERE Evidence_Key__c IN :keys WITH SYSTEM_MODE
        ]) {
            byKey.remove(row.Evidence_Key__c);
        }
        return byKey.values();
    }

    // --- Validation ---------------------------------------------------------------------

    @TestVisible
    static String problemWith(Command command) {
        if (command == null) {
            return 'Empty command.';
        }
        if (!matches(OPERATION_ID, command.operationId)) {
            return 'operationId has an invalid format.';
        }
        if (!OPERATIONS.contains(command.operation)) {
            return 'Unknown operation.';
        }
        if (!matches(CUSTOMER_ID, command.customerId)) {
            return 'customerId has an invalid format.';
        }
        if (command.conversationId != null && command.conversationId.length() > 64) {
            return 'conversationId is too long.';
        }
        if (command.operation == CREATE) {
            if (!matches(ISSUE_ID, command.issueId)) {
                return 'issueId has an invalid format.';
            }
            if (!CATEGORIES.contains(command.category)) {
                return 'Unknown category.';
            }
            if (String.isBlank(command.subject) || String.isBlank(command.summary)) {
                return 'A new case needs a subject and an English summary.';
            }
            if (!PRIORITY_RANK.containsKey(command.priority)) {
                return 'Unknown priority.';
            }
            if (String.isNotBlank(command.orderNumber) && !matches(ORDER_NUMBER, command.orderNumber)) {
                return 'orderNumber has an invalid format.';
            }
            if (command.confidence != null && (command.confidence < 0 || command.confidence > 1)) {
                return 'confidence must be between 0 and 1.';
            }
            if (!hasKind(command, 'customer_report')) {
                return 'A new case needs the customer\'s own words as evidence.';
            }
        } else {
            if (!isCaseId(command.caseId)) {
                return 'caseId is not a case ID.';
            }
            if (String.isBlank(command.expectedVersion)) {
                return 'expectedVersion is required.';
            }
            if (String.isNotBlank(command.priority) && !PRIORITY_RANK.containsKey(command.priority)) {
                return 'Unknown priority.';
            }
        }
        if (command.operation == REVISE_SUMMARY &&
            (String.isBlank(command.summary) || command.summary.length() > MAX_TEXT)) {
            return 'A summary revision needs nonblank English summary text within the field limit.';
        }
        if ((command.operation == REQUEST_CLOSURE || command.operation == CLOSE) && !hasKind(command, 'verified_fact')) {
            return 'Closing needs verified resolution evidence.';
        }
        if (command.operation == CLOSE) {
            if (!matches(REF, command.confirmationMessageId) || !hasConfirmation(command)) {
                return 'Closing needs the customer\'s confirmation message as customer_confirmation evidence.';
            }
        }
        if (String.isNotBlank(command.language) && !LANGUAGES.contains(command.language)) {
            return 'Unknown language.';
        }
        if (String.isNotBlank(command.sentiment) && !SENTIMENTS.contains(command.sentiment)) {
            return 'Unknown sentiment.';
        }
        if (String.isNotBlank(command.urgency) && !URGENCIES.contains(command.urgency)) {
            return 'Unknown urgency.';
        }
        if (String.isNotBlank(command.staffAction) && !STAFF_ACTIONS.contains(command.staffAction)) {
            return 'Unknown staff action.';
        }
        if (command.evidence != null) {
            if (command.evidence.size() > MAX_EVIDENCE) {
                return 'Too many evidence items.';
            }
            for (EvidenceItem item : command.evidence) {
                if (item == null || !matches(REF, item.ref) || !EVIDENCE_KINDS.contains(item.kind)
                        || String.isBlank(item.text)) {
                    return 'Each evidence item needs a valid ref, kind and text.';
                }
                if (item.kind == 'verified_fact' && String.isBlank(item.verifiedSource)) {
                    return 'A verified fact needs its source.';
                }
                if (String.isNotBlank(item.language) && !LANGUAGES.contains(item.language)) {
                    return 'Unknown evidence language.';
                }
                if (String.isNotBlank(item.script) && !SCRIPTS.contains(item.script)) {
                    return 'Unknown evidence script.';
                }
            }
        }
        return null;
    }

    static Boolean hasKind(Command command, String kind) {
        if (command.evidence != null) {
            for (EvidenceItem item : command.evidence) {
                if (item != null && item.kind == kind) {
                    return true;
                }
            }
        }
        return false;
    }

    static Boolean hasConfirmation(Command command) {
        for (EvidenceItem item : command.evidence == null ? new List<EvidenceItem>() : command.evidence) {
            if (item != null && item.kind == 'customer_confirmation' && command.confirmationMessageId.equals(item.ref)) {
                return true;
            }
        }
        return false;
    }

    // --- Helpers --------------------------------------------------------------------------

    static NABD_Operation_Receipt__c findReceipt(String operationId) {
        for (NABD_Operation_Receipt__c row : [
            SELECT Id, Operation_Id__c, Request_Fingerprint__c, Result_JSON__c FROM NABD_Operation_Receipt__c
            WHERE Operation_Id__c = :operationId WITH SYSTEM_MODE LIMIT 5
        ]) {
            if (operationId.equals(row.Operation_Id__c)) {
                return row;
            }
        }
        return null;
    }

    static Result stored(NABD_Operation_Receipt__c receipt) {
        return (Result) JSON.deserialize(receipt.Result_JSON__c, Result.class);
    }

    static NABD_Operation_Receipt__c receipt(Command command, String fingerprint, Result result) {
        return new NABD_Operation_Receipt__c(
            Operation_Id__c = command.operationId, Operation_Type__c = command.operation, Status__c = result.status,
            Case__c = result.caseId, Target_Record_Id__c = String.isNotBlank(command.caseId) ? command.caseId : result.caseId,
            Expected_Version__c = command.expectedVersion, Result_Version__c = result.caseVersion,
            Request_Fingerprint__c = fingerprint, Result_JSON__c = JSON.serialize(result), Completed_At__c = System.now()
        );
    }

    static Result outcome(Command command, String status, String reason, String message) {
        Result result = new Result();
        result.status = status;
        result.reason = reason;
        result.message = message;
        if (command != null) {
            result.operationId = command.operationId;
            result.operation = command.operation;
            result.customerId = command.customerId;
        }
        return result;
    }

    static Result refused(Command command, String reason, String message) {
        return outcome(command, 'failed', reason, message); // not recorded: nothing happened
    }

    static Result conflict(Command command, Case target, String reason, String message) {
        Result result = outcome(command, 'conflict', reason, message);
        return describe(result, target.Id);
    }

    static Result describe(Result result, Id caseId) {
        Case current = [SELECT Id, CaseNumber, Status, SystemModstamp, OwnerId FROM Case WHERE Id = :caseId WITH SYSTEM_MODE];
        result.caseId = current.Id;
        result.caseNumber = current.CaseNumber;
        result.caseStatus = current.Status;
        result.caseVersion = version(current.SystemModstamp);
        result.queue = queueName(current.OwnerId);
        return result;
    }

    static Id queueFor(String category) {
        for (NABD_Category_Route__mdt route : NABD_Category_Route__mdt.getAll().values()) {
            if (route.Category__c == category) {
                String developerName = route.Queue_Developer_Name__c;
                List<Group> queues = [
                    SELECT Id FROM Group WHERE Type = 'Queue' AND DeveloperName = :developerName
                    WITH SYSTEM_MODE LIMIT 1
                ];
                return queues.isEmpty() ? null : queues[0].Id;
            }
        }
        return null;
    }

    static String queueName(Id ownerId) {
        if (ownerId == null || ownerId.getSobjectType() != Group.SObjectType) {
            return null; // a staff member took the case
        }
        List<Group> queues = [SELECT DeveloperName FROM Group WHERE Id = :ownerId WITH SYSTEM_MODE LIMIT 1];
        return queues.isEmpty() ? null : queues[0].DeveloperName;
    }

    static Boolean staffActionPending(Case target) {
        return String.isNotBlank(target.Staff_Action_Required__c) && target.Staff_Action_Completed__c != true;
    }

    static String issuePrefix(Case target) {
        return String.isNotBlank(target.NABD_Issue_Id__c) ? target.NABD_Issue_Id__c : 'case:' + target.Id;
    }

    static String firstText(Command command, String kind) {
        for (EvidenceItem item : command.evidence) {
            if (item != null && item.kind == kind) {
                return item.text.left(MAX_TEXT);
            }
        }
        return null;
    }

    static Integer rank(String priority) {
        Integer value = priority == null ? null : PRIORITY_RANK.get(priority);
        return value == null ? 0 : value;
    }

    @TestVisible
    static String hashOf(String text) {
        if (text == null) {
            return null;
        }
        return EncodingUtil.convertToHex(Crypto.generateDigest('SHA-256', Blob.valueOf(text.replace('\r\n', '\n'))));
    }

    @TestVisible
    static String version(Datetime value) {
        return value.formatGmt('yyyy-MM-dd\'T\'HH:mm:ss.SSS\'Z\'');
    }

    static Boolean sameVersion(Datetime actual, String expected) {
        Datetime parsed = parseTime(expected);
        return parsed != null && actual.getTime() == parsed.getTime();
    }

    static Datetime parseTime(String value) {
        if (String.isBlank(value)) {
            return null;
        }
        try {
            return (Datetime) JSON.deserialize('"' + value + '"', Datetime.class);
        } catch (Exception error) {
            return null;
        }
    }

    static Boolean isCaseId(String value) {
        try {
            return value != null && Id.valueOf(value).getSobjectType() == Case.SObjectType;
        } catch (Exception error) {
            return false;
        }
    }

    static Boolean matches(Pattern pattern, String value) {
        return value != null && pattern.matcher(value).matches();
    }

    static String clip(String value, Integer length) {
        return value == null ? null : value.left(length);
    }
}
