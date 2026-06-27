"""SQLAlchemy engine, session factory, and declarative Base.

Tenant isolation (decision A1) is enforced two ways:
  * application WHERE-clauses (always filter by tenant_id), and
  * Postgres Row-Level Security (migration 0002), which reads the current tenant
    from the `app.current_tenant` GUC.

`get_tenant_db` is the request-scoped dependency that establishes the tenant for
RLS before any query runs. `get_db` remains available for migrations, startup,
and code paths that manage tenancy themselves.

WHY `SET LOCAL` AND NOT `SET`
-----------------------------
The engine uses a connection pool. `SET app.current_tenant = ...` (session-level)
persists on the physical connection *after* the request returns it to the pool —
the next request to grab that connection would inherit the previous request's
tenant. That is a cross-tenant data leak. `SET LOCAL` is scoped to the current
transaction and is automatically reset on COMMIT/ROLLBACK, so the value can never
outlive the request. We therefore ALWAYS use `SET LOCAL`, and we run it inside the
same transaction the queries use (otherwise the LOCAL value would be discarded
before the queries execute).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def set_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    """Bind the session's current transaction to a tenant for Postgres RLS.

    Issues `SET LOCAL app.current_tenant = '<uuid>'`. Because `SET LOCAL` only
    lives until the end of the current transaction, callers must run their queries
    within the same transaction (do not COMMIT/ROLLBACK before querying). The uuid
    is bound as a parameter (not f-string interpolated) to avoid any SQL injection
    surface, even though the value is server-controlled today.
    """
    # SET LOCAL does not accept a bind parameter for the value directly, so we use
    # set_config(name, value, is_local=true) which does and is exactly equivalent.
    session.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(tenant_id)},
    )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_tenant_db() -> Iterator[Session]:
    """Request-scoped session with the current tenant established for RLS.

    `set_tenant` is issued as the session's FIRST statement. That statement
    autobegins the SQLAlchemy transaction, so the `SET LOCAL` lands inside the very
    transaction the request's queries then run in — the hard requirement for
    `SET LOCAL` to take effect. Downstream code is free to `commit()` (the ingest
    tail does); when it does, that one transaction — tenant setting and all — is
    finalized and the LOCAL value is discarded. `db.close()` then rolls back any
    still-open transaction, so the pooled connection is always returned clean with
    no lingering `app.current_tenant`.

    NOTE: we deliberately do NOT wrap this in `with db.begin()`: downstream code
    (e.g. app/services/connectors/base.py:ingest) calls `db.commit()` itself, which
    would close the context-manager's transaction out from under it and raise.

    Slice 0 uses the single default tenant from settings; real per-request tenancy
    (e.g. from the authenticated principal) layers in here later.
    """
    db = SessionLocal()
    try:
        set_tenant(db, get_settings().tenant_uuid)
        yield db
    finally:
        db.close()
