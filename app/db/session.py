"""SQLAlchemy engine, session factory, and declarative Base.

Slice 0 is single-tenant on DEFAULT_TENANT_ID; RLS enforcement (A1) lands in the
hardening slice. Every row still carries tenant_id so that flip is a no-op for
business logic.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
