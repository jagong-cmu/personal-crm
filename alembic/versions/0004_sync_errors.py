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


def downgrade() -> None:
    op.drop_index("ix_sync_errors_tenant_id", table_name="sync_errors")
    op.drop_table("sync_errors")
