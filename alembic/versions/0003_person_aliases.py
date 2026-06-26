"""person_aliases (T3)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-25

Sticky source-record -> person merges. Resolution (T4) consults this table FIRST, so a
manual or confident-auto merge persists across re-syncs. Un-merge = delete the row.

down_revision is "0002" (T1's RLS migration, built by a parallel agent — referenced by
string id only).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "person_aliases",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("person_id", _UUID, sa.ForeignKey("people.id"), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),  # 'manual' | 'auto'
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "tenant_id", "source_type", "source_record_id", name="uq_person_alias"
        ),
    )
    op.create_index("ix_person_aliases_tenant_id", "person_aliases", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_person_aliases_tenant_id", table_name="person_aliases")
    op.drop_table("person_aliases")
