"""Integration tests for T1 Postgres Row-Level Security tenant isolation.

These prove the DATABASE itself (not just application WHERE-clauses) blocks
cross-tenant access:
  * tenant A cannot SELECT tenant B's rows, even via a raw SQL query;
  * tenant A cannot INSERT a row carrying tenant B's tenant_id (WITH CHECK);
  * a transaction that never sets `app.current_tenant` fails closed (it errors,
    rather than silently returning all rows).

REQUIRES A LIVE POSTGRES with migrations applied through revision 0002:

    docker compose up -d db
    alembic upgrade head
    DATABASE_URL=postgresql+psycopg://... pytest tests/test_rls.py

The whole module SKIPS (never fails) when no Postgres is reachable or when the
RLS policy is absent, so it is safe to collect in environments without a DB.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

# DATABASE_URL env var wins; otherwise fall back to app settings if importable.
_DB_URL = os.environ.get("DATABASE_URL")
if not _DB_URL:
    try:  # pragma: no cover - convenience only
        from app.config import get_settings

        _DB_URL = get_settings().database_url
    except Exception:  # noqa: BLE001
        _DB_URL = None


def _engine_or_skip():
    if not _DB_URL:
        pytest.skip("No DATABASE_URL / app settings; live Postgres required for RLS tests")
    try:
        eng = create_engine(_DB_URL, pool_pre_ping=True, future=True)
        with eng.connect() as conn:
            policy = conn.execute(
                text(
                    "SELECT 1 FROM pg_policies "
                    "WHERE tablename = 'people' AND policyname = 'tenant_isolation'"
                )
            ).first()
        if policy is None:
            pytest.skip("RLS policy 'tenant_isolation' missing; run `alembic upgrade head`")
        return eng
    except pytest.skip.Exception:
        raise
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip, not fail
        pytest.skip(f"Postgres unreachable ({exc!r}); RLS tests need a live DB")


@pytest.fixture(scope="module")
def engine():
    eng = _engine_or_skip()
    yield eng
    eng.dispose()


def _set_tenant(conn, tenant_id: uuid.UUID) -> None:
    # SET LOCAL via set_config(is_local=true): scoped to this transaction only.
    conn.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_id)},
    )


def _insert_person(conn, tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO people (id, tenant_id, primary_email) "
            "VALUES (:id, :tid, :email)"
        ),
        {"id": pid, "tid": tenant_id, "email": email},
    )
    return pid


@pytest.fixture()
def two_tenants(engine):
    """Create one person in tenant A and one in tenant B, each under its own
    tenant context (FORCE RLS means inserts honour WITH CHECK). Cleaned up after."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    tag = uuid.uuid4().hex
    email_a = f"a-{tag}@rls.test"
    email_b = f"b-{tag}@rls.test"

    with engine.begin() as conn:
        _set_tenant(conn, tenant_a)
        id_a = _insert_person(conn, tenant_a, email_a)
    with engine.begin() as conn:
        _set_tenant(conn, tenant_b)
        id_b = _insert_person(conn, tenant_b, email_b)

    yield {
        "a": tenant_a,
        "b": tenant_b,
        "id_a": id_a,
        "id_b": id_b,
        "email_a": email_a,
        "email_b": email_b,
    }

    # Teardown: each tenant deletes its own row (RLS applies to DELETE too).
    for tenant, pid in ((tenant_a, id_a), (tenant_b, id_b)):
        with engine.begin() as conn:
            _set_tenant(conn, tenant)
            conn.execute(text("DELETE FROM people WHERE id = :id"), {"id": pid})


def test_cross_tenant_select_blocked_via_raw_query(engine, two_tenants):
    """Tenant A, issuing a bare `SELECT ... FROM people` (no WHERE on tenant_id),
    sees only its own row. The DB — not the app — filters out tenant B."""
    with engine.begin() as conn:
        _set_tenant(conn, two_tenants["a"])
        ids = {r[0] for r in conn.execute(text("SELECT id FROM people")).fetchall()}

    assert two_tenants["id_a"] in ids, "tenant A must see its own row"
    assert two_tenants["id_b"] not in ids, "tenant A must NOT see tenant B's row"


def test_cross_tenant_lookup_by_email_blocked(engine, two_tenants):
    """Even an exact lookup of tenant B's email under tenant A's context returns
    nothing — RLS strips the row before the WHERE-clause matches."""
    with engine.begin() as conn:
        _set_tenant(conn, two_tenants["a"])
        row = conn.execute(
            text("SELECT id FROM people WHERE primary_email = :e"),
            {"e": two_tenants["email_b"]},
        ).first()
    assert row is None


def test_cross_tenant_insert_blocked_by_with_check(engine, two_tenants):
    """Under tenant A's context, inserting a row stamped with tenant B's id is
    rejected by the policy's WITH CHECK clause."""
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            _set_tenant(conn, two_tenants["a"])
            _insert_person(conn, two_tenants["b"], f"evil-{uuid.uuid4().hex}@rls.test")


def test_unset_tenant_fails_closed(engine, two_tenants):
    """A transaction that never sets app.current_tenant must NOT see all rows.

    Our policy uses `current_setting('app.current_tenant')::uuid` (no missing_ok),
    so an unset GUC raises 'unrecognized configuration parameter' and the query
    aborts — fail-closed."""
    with pytest.raises((ProgrammingError, DBAPIError)):
        with engine.begin() as conn:
            # Intentionally NO _set_tenant() here.
            conn.execute(text("SELECT count(*) FROM people")).scalar()
