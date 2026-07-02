"""Discover (contact generator): user_profile + prospect tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-30

Adds the two tables backing the Discover feature:
  * user_profile — the user's OWN background/interests (one row per tenant).
  * prospect     — generated outreach candidates, kept SEPARATE from the real
                   network (people) until promoted via base.ingest.

Both are tenant-scoped, so they get the same fail-closed RLS treatment as the core
tables (see 0002_rls.py): ENABLE + FORCE ROW LEVEL SECURITY + a tenant_isolation
policy. DML grants to the runtime 'crm_app' role are inherited automatically from the
ALTER DEFAULT PRIVILEGES in scripts/setup_app_role.sql, provided this migration runs as
the owning role (the documented ADMIN_DATABASE_URL setup).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB

# Same fail-closed tenant policy as 0002 (errors if app.current_tenant is unset).
_TENANT_EXPR = "current_setting('app.current_tenant')::uuid"
_RLS_TABLES = ("user_profile", "prospect")


def _apply_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {_TENANT_EXPR}) "
        f"WITH CHECK (tenant_id = {_TENANT_EXPR})"
    )


def upgrade() -> None:
    op.create_table(
        "user_profile",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("headline", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("schools", _JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("companies", _JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skills", _JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("about", sa.Text()),
        sa.Column("raw", _JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", name="uq_user_profile_tenant"),
    )
    op.create_index("ix_user_profile_tenant_id", "user_profile", ["tenant_id"])

    op.create_table(
        "prospect",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("company", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("school", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("score_breakdown", _JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("relation_summary", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("promoted_person_id", _UUID, sa.ForeignKey("people.id")),
        sa.Column("raw", _JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_prospect_dedupe"),
    )
    op.create_index("ix_prospect_tenant_id", "prospect", ["tenant_id"])
    op.create_index("ix_prospect_tenant_status", "prospect", ["tenant_id", "status"])
    op.create_index("ix_prospect_tenant_score", "prospect", ["tenant_id", "score"])

    for table in _RLS_TABLES:
        _apply_rls(table)


def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_prospect_tenant_score", table_name="prospect")
    op.drop_index("ix_prospect_tenant_status", table_name="prospect")
    op.drop_index("ix_prospect_tenant_id", table_name="prospect")
    op.drop_table("prospect")

    op.drop_index("ix_user_profile_tenant_id", table_name="user_profile")
    op.drop_table("user_profile")
