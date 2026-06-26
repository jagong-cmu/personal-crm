"""Postgres Row-Level Security tenant isolation (T1, hardening slice)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-25

Defense-in-depth tenant isolation (decision A1). Application code already filters
by tenant_id in WHERE-clauses; this migration makes the *database* enforce the same
boundary so a missing/incorrect WHERE-clause can never leak cross-tenant rows.

For every tenant-scoped table we:
  1. ENABLE ROW LEVEL SECURITY
  2. FORCE ROW LEVEL SECURITY  -- see note below
  3. CREATE POLICY tenant_isolation USING/WITH CHECK (tenant_id = <current tenant>)

The current tenant is read from the `app.current_tenant` GUC, which the request
layer sets per-transaction via `SET LOCAL app.current_tenant = '<uuid>'`
(see app/db/session.py).

WHY FORCE IS REQUIRED
---------------------
The application connects as the DB owner/superuser by default. Table *owners*
(and superusers) BYPASS row-level security policies unless `FORCE ROW LEVEL
SECURITY` is set on the table. Without FORCE, RLS would silently do nothing for
the very role our app uses. FORCE makes the policy apply to the owner too.

FAIL-CLOSED DECISION (unset tenant)
-----------------------------------
The policy uses `current_setting('app.current_tenant')::uuid` WITHOUT the
`missing_ok` (second) argument. If `app.current_tenant` has never been set in the
session/transaction, `current_setting` raises
`ERROR: unrecognized configuration parameter "app.current_tenant"`, which aborts
the whole statement. This is the strongest fail-closed behaviour: you cannot read
or write any row without first establishing a tenant. A NULL/missing setting can
therefore never "match all rows".

(The alternative, `current_setting('app.current_tenant', true)::uuid`, returns
NULL when unset; `tenant_id = NULL` evaluates to NULL -> the row is filtered out,
so that is *also* fail-closed and returns zero rows. We deliberately chose the
erroring form so an un-scoped query is a loud bug rather than a silently empty
result set.)

INDEXES
-------
The decision calls for tenant_id to be the leading column of every index. The
single-column `ix_*_tenant_id` indexes from 0001 are already tenant-leading. The
two secondary lookup indexes (`primary_email` on people, `domain` on
organizations) are NOT tenant-leading; under RLS every lookup is implicitly
tenant-scoped, so we add tenant-leading composite indexes for them.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Tenant-scoped tables that carry a tenant_id column (all six core tables).
_TENANT_TABLES = (
    "people",
    "person_sources",
    "organizations",
    "interactions",
    "embedding_chunks",
    "oauth_credentials",
)

# Fail-closed expression: errors if app.current_tenant is unset (see docstring).
_TENANT_EXPR = "current_setting('app.current_tenant')::uuid"


def upgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the owner/superuser our app connects as is also subject to RLS.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {_TENANT_EXPR}) "
            f"WITH CHECK (tenant_id = {_TENANT_EXPR})"
        )

    # Tenant-leading composite indexes for the non-tenant-leading lookup columns.
    op.create_index(
        "ix_people_tenant_primary_email", "people", ["tenant_id", "primary_email"]
    )
    op.create_index(
        "ix_organizations_tenant_domain", "organizations", ["tenant_id", "domain"]
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_tenant_domain", table_name="organizations")
    op.drop_index("ix_people_tenant_primary_email", table_name="people")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
