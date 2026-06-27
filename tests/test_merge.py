"""Integration tests for manual merge / un-merge (T14).

Proves: a merge repoints all of the loser's records to the winner, tombstones the loser,
and writes sticky aliases so a re-synced loser record resolves to the WINNER (never
re-splits). Un-merge reverses exactly those records.

Skips (never fails) when no live Postgres with migrations through 0005 is available.
"""
from __future__ import annotations

import os
import uuid

import pytest

_DB_URL = os.environ.get("DATABASE_URL")
if not _DB_URL:
    try:  # pragma: no cover
        from app.config import get_settings

        _DB_URL = get_settings().database_url
    except Exception:  # noqa: BLE001
        _DB_URL = None


@pytest.fixture()
def db_tenant():
    if not _DB_URL:
        pytest.skip("No DATABASE_URL; merge tests need a live Postgres")
    from sqlalchemy import text

    from app.db.session import SessionLocal, engine, set_tenant

    try:
        with engine.connect() as conn:
            ok = conn.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name='people' "
                     "AND column_name='merged_into_id'")
            ).first()
        if ok is None:
            pytest.skip("migrations not applied through 0005; run `alembic upgrade head`")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({exc!r})")

    tenant = uuid.uuid4()
    db = SessionLocal()
    set_tenant(db, tenant)
    yield db, tenant
    from app.models import EmbeddingChunk, Interaction, Person, PersonAlias, PersonSource

    for model in (PersonAlias, EmbeddingChunk, Interaction, PersonSource):
        db.query(model).filter(model.tenant_id == tenant).delete()
    db.query(Person).filter(Person.tenant_id == tenant).update({"merged_into_id": None})
    db.query(Person).filter(Person.tenant_id == tenant).delete()
    db.commit()
    db.close()


def _seed_pair(db, tenant):
    from app.models import EmbeddingChunk, Interaction, Person, PersonSource

    winner = Person(tenant_id=tenant, display_name="Sarah Chen", primary_email="sarah@stripe.com", company="Stripe")
    loser = Person(tenant_id=tenant, display_name="S. Chen", primary_email="s.chen@gmail.com", company="Stripe")
    db.add_all([winner, loser])
    db.flush()
    db.add(PersonSource(tenant_id=tenant, person_id=loser.id, source_type="contacts", source_record_id="c:L"))
    db.add(EmbeddingChunk(
        tenant_id=tenant, person_id=loser.id, source_type="contacts", source_record_id="c:L",
        chunk_text="S. Chen — Stripe", content_hash="h:L", embedding=[0.0] * 1024,
    ))
    db.add(Interaction(
        tenant_id=tenant, person_id=loser.id, source_type="contacts",
        interaction_type="email_sent", source_record_id="c:L",
    ))
    db.commit()
    return winner, loser


def test_merge_repoints_tombstones_and_sticks(db_tenant):
    from app.models import EmbeddingChunk, Interaction, PersonAlias, PersonSource
    from app.services import entity_resolution, merge
    from app.services.connectors.base import NormalizedRecord

    db, tenant = db_tenant
    winner, loser = _seed_pair(db, tenant)

    out = merge.merge(db, tenant, winner.id, loser.id)
    assert out["moved_sources"] == 1

    db.refresh(loser)
    assert loser.merged_into_id == winner.id  # tombstoned

    # All of the loser's records now point at the winner.
    for model in (PersonSource, EmbeddingChunk, Interaction):
        assert db.query(model).filter(model.tenant_id == tenant, model.person_id == loser.id).count() == 0
        assert db.query(model).filter(model.tenant_id == tenant, model.person_id == winner.id).count() >= 1

    alias = db.query(PersonAlias).filter(
        PersonAlias.tenant_id == tenant, PersonAlias.source_record_id == "c:L"
    ).one()
    assert alias.person_id == winner.id and alias.merged_from_id == loser.id

    # Stickiness: re-resolving the loser's source record returns the WINNER (alias-first).
    res = entity_resolution.resolve(
        db, tenant,
        NormalizedRecord(source_type="contacts", source_record_id="c:L",
                         display_name="S. Chen", primary_email="s.chen@gmail.com"),
    )
    assert res.person.id == winner.id and not res.created


def test_unmerge_restores(db_tenant):
    from app.models import EmbeddingChunk, Interaction, PersonAlias, PersonSource
    from app.services import merge

    db, tenant = db_tenant
    winner, loser = _seed_pair(db, tenant)
    merge.merge(db, tenant, winner.id, loser.id)

    out = merge.unmerge(db, tenant, loser.id)
    assert out["restored_sources"] == 1

    db.refresh(loser)
    assert loser.merged_into_id is None
    for model in (PersonSource, EmbeddingChunk, Interaction):
        assert db.query(model).filter(model.tenant_id == tenant, model.person_id == loser.id).count() >= 1
    assert db.query(PersonAlias).filter(
        PersonAlias.tenant_id == tenant, PersonAlias.merged_from_id == loser.id
    ).count() == 0


def test_cannot_merge_into_self(db_tenant):
    from app.services import merge

    db, tenant = db_tenant
    winner, _ = _seed_pair(db, tenant)
    with pytest.raises(merge.MergeError):
        merge.merge(db, tenant, winner.id, winner.id)
