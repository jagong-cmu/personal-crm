"""FastAPI entrypoint.

Startup assertions, both fail fast at boot:
  * (A3) the configured VOYAGE_DIM must equal the actual embedding column dimension.
  * (A1) the runtime DB role must NOT bypass Row-Level Security. A superuser (or any
    BYPASSRLS role) makes every tenant-isolation policy a silent no-op, so we refuse to
    start as one rather than leak across tenants.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api import auth, people, query, sync
from app.config import get_settings
from app.db.session import engine


def _assert_embedding_dim() -> None:
    s = get_settings()
    with engine.connect() as conn:
        col_type = conn.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'embedding_chunks'::regclass AND attname = 'embedding'"
            )
        ).scalar()
    # col_type looks like 'vector(1024)'
    if not col_type:
        raise RuntimeError("embedding_chunks.embedding column not found — run migrations.")
    actual = int(col_type.strip().removeprefix("vector(").removesuffix(")"))
    if actual != s.voyage_dim:
        raise RuntimeError(
            f"Embedding dim mismatch: VOYAGE_DIM={s.voyage_dim} but DB column is "
            f"{actual}. Fix config or migrate."
        )


def _assert_rls_enforced() -> None:
    """Refuse to start if the runtime role bypasses RLS (superuser or BYPASSRLS).

    Postgres `FORCE ROW LEVEL SECURITY` still does not constrain a superuser or a
    BYPASSRLS role, so connecting as one turns tenant isolation into a no-op. Connect
    as the non-superuser `crm_app` role instead (auto-created by docker-compose; see
    scripts/setup_app_role.sql / README).
    """
    with engine.connect() as conn:
        bypasses = conn.execute(
            text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).scalar()
    if bypasses:
        raise RuntimeError(
            "Runtime DB role bypasses Row-Level Security — tenant isolation would be a "
            "no-op. Point DATABASE_URL at the non-superuser 'crm_app' role (run "
            "migrations via ADMIN_DATABASE_URL). See README step 5."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_rls_enforced()
    _assert_embedding_dim()
    yield


app = FastAPI(title="Network Intelligence Platform", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(sync.router)
app.include_router(query.router)
app.include_router(people.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
