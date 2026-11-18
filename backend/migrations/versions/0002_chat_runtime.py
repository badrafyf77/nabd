"""Chat runtime state: demo sessions, conversations, messages, runs and events."""
from alembic import op

revision = "0002_chat_runtime"
down_revision = "0001_nabd_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE nabd.demo_sessions (
            token_hash bytea PRIMARY KEY,
            customer_id text NOT NULL,
            first_name text NOT NULL,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            ended_at timestamptz
        );

        CREATE TABLE nabd.conversations (
            id uuid PRIMARY KEY,
            customer_id text NOT NULL,
            customer_name text NOT NULL,
            title text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            last_sequence bigint NOT NULL DEFAULT 0
        );
        CREATE INDEX conversations_by_customer ON nabd.conversations (customer_id, updated_at DESC);

        CREATE TABLE nabd.messages (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            role text NOT NULL CHECK (role IN ('customer', 'assistant')),
            client_message_id text,
            run_id uuid,
            text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (conversation_id, client_message_id)
        );
        CREATE INDEX messages_by_conversation ON nabd.messages (conversation_id, created_at);

        CREATE TABLE nabd.runs (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            message_id uuid NOT NULL UNIQUE REFERENCES nabd.messages (id),
            turn integer NOT NULL,
            status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
            attempt integer NOT NULL DEFAULT 0,
            lease_owner text,
            lease_expires_at timestamptz,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            UNIQUE (conversation_id, turn)
        );
        CREATE INDEX runs_pending ON nabd.runs (conversation_id, turn) WHERE status IN ('queued', 'running');

        CREATE TABLE nabd.events (
            conversation_id uuid NOT NULL REFERENCES nabd.conversations (id),
            sequence bigint NOT NULL,
            id uuid NOT NULL UNIQUE,
            run_id uuid,
            type text NOT NULL,
            activity_id text,
            parent_activity_id text,
            state text,
            code text,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (conversation_id, sequence)
        );
    """)


def downgrade() -> None:
    # Never silently discard customer conversations.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM nabd.conversations) THEN
                RAISE EXCEPTION 'refusing to drop non-empty NABD conversation tables';
            END IF;
        END $$;
        DROP TABLE nabd.events;
        DROP TABLE nabd.runs;
        DROP TABLE nabd.messages;
        DROP TABLE nabd.conversations;
        DROP TABLE nabd.demo_sessions;
    """)
