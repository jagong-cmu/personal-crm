"""sync_state cursors (T13) + people.merged_into_id merge tombstone (T14)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-26

Two additions for the sources + merge slices:

  * ``sync_state`` — one row per (tenant, source) holding the incremental-sync cursor
    (Gmail historyId / Calendar syncToken). RLS-isolated like every tenant table.
  * ``people.merged_into_id`` — nullable self-FK. Set when a person is manually merged
    INTO another (the loser is tombstoned, not deleted, so the merge is reversible).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)

# Same fail-closed tenant policy as 0002 (errors if app.current_tenant is unset).
_TENANT_EXPR = "current_setting('app.current_tenant')::uuid"


def upgrade() -> None:
    # --- people.merged_into_id (merge tombstone) ---
    op.add_column(
        "people",
        sa.Column(
            "merged_into_id",
            _UUID,
            sa.ForeignKey("people.id"),
            nullable=True,
        ),
    )

    # --- person_aliases.merged_from_id (T14: reversible manual merge provenance) ---
    op.add_column(
        "person_aliases",
        sa.Column("merged_from_id", _UUID, sa.ForeignKey("people.id"), nullable=True),
    )

    # --- sync_state (incremental cursors) ---
    op.create_table(
        "sync_state",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("cursor", sa.Text()),
        sa.Column("last_synced_at", _TS),
        sa.Column("updated_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "source_type", name="uq_sync_state_source"),
    )
    op.create_index("ix_sync_state_tenant_id", "sync_state", ["tenant_id"])

    op.execute("ALTER TABLE sync_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sync_state FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON sync_state "
        f"USING (tenant_id = {_TENANT_EXPR}) "
        f"WITH CHECK (tenant_id = {_TENANT_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sync_state")
    op.execute("ALTER TABLE sync_state NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sync_state DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_sync_state_tenant_id", table_name="sync_state")
    op.drop_table("sync_state")

    op.drop_column("person_aliases", "merged_from_id")
    op.drop_column("people", "merged_into_id")
