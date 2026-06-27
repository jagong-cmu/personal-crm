"""FastAPI entrypoint.

Startup assertion (decision A3): the configured VOYAGE_DIM must equal the actual
embedding column dimension in Postgres. A mismatch fails fast at boot instead of
silently erroring on insert.
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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
