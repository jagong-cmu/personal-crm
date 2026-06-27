"""Per-record ingest isolation against a real Postgres (T7).

Skipped automatically when no usable Postgres/pgvector is reachable, so it never
blocks the offline test run. Verifies that one poisoned record is dead-lettered
into sync_errors while the good records still commit, and that the dead-letter row
never contains the raw record body/PII.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.db.session import SessionLocal, engine, set_tenant
from app.models import EmbeddingChunk, Person, PersonSource, SyncError
from app.services.connectors import base as base_mod
from app.services.connectors.base import NormalizedRecord, ingest


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="no Postgres reachable (offline / no DATABASE_URL)"
)


def test_one_bad_record_is_isolated(monkeypatch):
    tenant_id = uuid.uuid4()  # isolate this run's rows

    # Avoid network: fake the embedding call to a deterministic vector.
    from app.config import get_settings

    d = get_settings().voyage_dim
    monkeypatch.setattr(base_mod, "embed_documents", lambda texts: [[0.1] * d for _ in texts])

    SECRET = "TOP-SECRET-BODY-should-never-be-stored"
    records = [
        NormalizedRecord(
            source_type="contacts",
            source_record_id="good-1",
            display_name="Ada Lovelace",
            primary_email="ada@example.com",
            text="Ada Lovelace, mathematician",
            raw={"body": SECRET},
        ),
        NormalizedRecord(
            source_type="contacts",
            source_record_id="bad-1",
            display_name="Boom",
            primary_email="boom@example.com",
            text="boom",
            raw={"body": SECRET},
        ),
    ]

    # Poison resolution for exactly the bad record. ingest() calls
    # entity_resolution.resolve directly (to capture fuzzy confidence), so patch THERE.
    from app.services import entity_resolution

    real_resolve = entity_resolution.resolve

    def flaky_resolve(db, tid, rec, *args, **kwargs):
        if rec.source_record_id == "bad-1":
            raise RuntimeError("simulated resolve failure")
        return real_resolve(db, tid, rec, *args, **kwargs)

    monkeypatch.setattr(entity_resolution, "resolve", flaky_resolve)

    db = SessionLocal()
    set_tenant(db, tenant_id)  # required under enforced RLS (non-superuser role)
    try:
        result = ingest(db, tenant_id, records)

        assert result.errors == 1
        assert result.sources_upserted == 1

        people = db.scalars(select(Person).where(Person.tenant_id == tenant_id)).all()
        assert {p.primary_email for p in people} == {"ada@example.com"}

        errs = db.scalars(select(SyncError).where(SyncError.tenant_id == tenant_id)).all()
        assert len(errs) == 1
        err = errs[0]
        assert err.source_record_id == "bad-1"
        assert err.stage == "resolve"
        # Privacy guarantee: the secret body never leaks into the dead-letter row.
        assert SECRET not in err.reason
        assert "RuntimeError" in err.reason

        chunks = db.scalars(
            select(EmbeddingChunk).where(EmbeddingChunk.tenant_id == tenant_id)
        ).all()
        assert len(chunks) == 1  # only the good record embedded
    finally:
        # Clean up this run's rows.
        for model in (EmbeddingChunk, SyncError, PersonSource):
            db.query(model).filter(model.tenant_id == tenant_id).delete()
        db.query(Person).filter(Person.tenant_id == tenant_id).delete()
        db.commit()
        db.close()
