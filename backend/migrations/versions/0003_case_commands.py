"""Case work state: each run's complaint plan, its durable case commands, and issue links."""
from alembic import op

revision = "0003_case_commands"
down_revision = "0002_chat_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        -- The verified complaint assessment of one run, saved before any command is sent,
        -- so a retried run reuses it instead of asking the model again.
        CREATE TABLE nabd.run_complaints (
            run_id uuid PRIMARY KEY REFERENCES nabd.runs (id),
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            customer_id text NOT NULL,
            assessment jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );

        -- One Salesforce case operation. Stored before dispatch; its operation ID and exact
        -- request never change, so retries and reconciliation cannot repeat the side effect.
        CREATE TABLE nabd.case_commands (
            operation_id text PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES nabd.runs (id),
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            customer_id text NOT NULL,
            position text NOT NULL,
            issue_ref text NOT NULL,
            operation text NOT NULL CHECK (operation IN
                ('create_case', 'update_case', 'reopen_case', 'request_closure', 'close_case')),
            request jsonb NOT NULL,
            card jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL CHECK (status IN
                ('pending', 'dispatched', 'unknown', 'confirmed', 'conflict', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            result jsonb,
            error_code text,
            reconcile_lease_until timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, position)
        );
        CREATE INDEX case_commands_open ON nabd.case_commands (updated_at)
            WHERE status IN ('pending', 'dispatched', 'unknown');

        -- Cases a conversation created or changed, as Salesforce last confirmed them.
        CREATE TABLE nabd.issue_links (
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            case_id text NOT NULL,
            customer_id text NOT NULL,
            case_number text NOT NULL,
            department text,
            status text,
            last_operation text NOT NULL,
            run_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (conversation_id, case_id)
        );
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM nabd.case_commands) THEN
                RAISE EXCEPTION 'refusing to drop non-empty NABD case command tables';
            END IF;
        END $$;
        DROP TABLE nabd.issue_links;
        DROP TABLE nabd.case_commands;
        DROP TABLE nabd.run_complaints;
    """)
