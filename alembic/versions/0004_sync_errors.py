"""sync_errors dead-letter table (T7)

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-25

Per-record ingest failures are isolated into this dead-letter table instead of
aborting the whole sync (decision T7). Privacy: rows hold the source record id +
a short reason string ONLY — never raw bodies, tokens, or PII payloads.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)

# Same fail-closed tenant policy as 0002 (errors if app.current_tenant is unset).
_TENANT_EXPR = "current_setting('app.current_tenant')::uuid"


def upgrade() -> None:
    op.create_table(
        "sync_errors",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text()),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
    )
    op.create_index("ix_sync_errors_tenant_id", "sync_errors", ["tenant_id"])

    # RLS: dead-letter rows are tenant-scoped, so isolate them like the core tables
    # (FORCE so the owner role our app uses is also subject). The ingest path writes
    # here under the same SET LOCAL app.current_tenant as the rest of the sync.
    op.execute("ALTER TABLE sync_errors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sync_errors FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON sync_errors "
        f"USING (tenant_id = {_TENANT_EXPR}) "
        f"WITH CHECK (tenant_id = {_TENANT_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sync_errors")
    op.execute("ALTER TABLE sync_errors NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sync_errors DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_sync_errors_tenant_id", table_name="sync_errors")
    op.drop_table("sync_errors")
