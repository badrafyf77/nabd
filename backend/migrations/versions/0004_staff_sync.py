"""Staff follow-up state: Salesforce replay cursors, cases to re-sync, delivered staff replies."""
from alembic import op

revision = "0004_staff_sync"
down_revision = "0003_case_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        -- Where the Salesforce change stream resumes: the replay ID of the last event whose
        -- "case needs sync" marker was durably stored.
        CREATE TABLE nabd.sync_cursors (
            topic text PRIMARY KEY,
            replay_id bytea NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        );

        -- Cases whose current Salesforce state must be re-read and shown to their chats. A newer
        -- request raises the generation, so a sync that read older state cannot clear it.
        CREATE TABLE nabd.case_syncs (
            case_id text PRIMARY KEY,
            generation bigint NOT NULL DEFAULT 1,
            reason text NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            source_at timestamptz,
            not_before timestamptz NOT NULL DEFAULT now(),
            attempts integer NOT NULL DEFAULT 0,
            lease_until timestamptz,
            lease_token uuid,
            last_error text
        );
        CREATE INDEX case_syncs_due ON nabd.case_syncs (not_before);

        -- What each linked conversation was last shown of its case, beyond status.
        ALTER TABLE nabd.issue_links
            ADD COLUMN staff_action text,
            ADD COLUMN staff_action_completed boolean,
            ADD COLUMN removed_at timestamptz,
            ADD COLUMN synced_at timestamptz;
        CREATE INDEX issue_links_by_case ON nabd.issue_links (case_id);

        -- Public staff replies (Salesforce case comments) delivered to a conversation, with
        -- their labeled translation. Withdrawn replies stay as an audit row without text.
        CREATE TABLE nabd.staff_replies (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            comment_id text NOT NULL,
            case_id text NOT NULL,
            case_number text NOT NULL,
            body text NOT NULL,
            revision integer NOT NULL DEFAULT 1,
            posted_at timestamptz NOT NULL,
            translation_status text NOT NULL CHECK (translation_status IN ('none', 'pending', 'done', 'failed')),
            target_language jsonb,
            translation jsonb,
            translation_attempts integer NOT NULL DEFAULT 0,
            translation_lease_until timestamptz,
            translation_token uuid,
            after_message_id uuid,
            withdrawn_at timestamptz,
            source_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (conversation_id, comment_id)
        );
        CREATE INDEX staff_replies_by_conversation ON nabd.staff_replies (conversation_id, created_at);
        CREATE INDEX staff_replies_translating ON nabd.staff_replies (updated_at)
            WHERE translation_status = 'pending';
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM nabd.staff_replies) THEN
                RAISE EXCEPTION 'refusing to drop non-empty NABD staff reply table';
            END IF;
        END $$;
        DROP TABLE nabd.staff_replies;
        DROP INDEX nabd.issue_links_by_case;
        ALTER TABLE nabd.issue_links DROP COLUMN staff_action, DROP COLUMN staff_action_completed,
            DROP COLUMN removed_at, DROP COLUMN synced_at;
        DROP TABLE nabd.case_syncs;
        DROP TABLE nabd.sync_cursors;
    """)
