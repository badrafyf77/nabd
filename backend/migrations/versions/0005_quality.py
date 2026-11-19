"""Restricted turn traces, quality reviews and one correction per source command."""
from alembic import op

revision = "0005_quality"
down_revision = "0004_staff_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE nabd.run_traces (
            run_id uuid NOT NULL REFERENCES nabd.runs (id),
            attempt integer NOT NULL,
            position integer NOT NULL,
            partial boolean NOT NULL,
            agent text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('model', 'tool', 'attempt')),
            name text NOT NULL,
            arguments jsonb,
            result jsonb,
            usage jsonb,
            duration_ms integer,
            PRIMARY KEY (run_id, attempt, position)
        );

        CREATE TABLE nabd.quality_reviews (
            run_id uuid PRIMARY KEY REFERENCES nabd.runs (id),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'graded', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            lease_token uuid,
            lease_until timestamptz,
            not_before timestamptz NOT NULL DEFAULT now(),
            grade jsonb,
            usage jsonb,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX quality_reviews_due ON nabd.quality_reviews (not_before) WHERE status = 'pending';

        CREATE TABLE nabd.summary_revisions (
            operation_id text PRIMARY KEY,
            source_command text NOT NULL UNIQUE REFERENCES nabd.case_commands (operation_id),
            case_id text NOT NULL,
            assessment jsonb NOT NULL,
            request jsonb,
            dispatched boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'conflict', 'rejected', 'failed')),
            attempts integer NOT NULL DEFAULT 0,
            lease_token uuid,
            lease_until timestamptz,
            not_before timestamptz NOT NULL DEFAULT now(),
            result jsonb,
            usage jsonb,
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX summary_revisions_due ON nabd.summary_revisions (not_before) WHERE status = 'pending';
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM nabd.run_traces) OR EXISTS (SELECT 1 FROM nabd.quality_reviews)
                OR EXISTS (SELECT 1 FROM nabd.summary_revisions) THEN
                RAISE EXCEPTION 'refusing to drop non-empty NABD quality tables';
            END IF;
        END $$;
        DROP TABLE nabd.summary_revisions, nabd.quality_reviews, nabd.run_traces;
    """)
