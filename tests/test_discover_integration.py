"""Integration tests for the Discover orchestrator (needs a live Postgres w/ 0007).

Mocks all external I/O (Brave/Hunter provider, Voyage embeddings, Claude relation
synthesis) so the DB path — save profile → run → dedupe → promote → RLS — is exercised
without any network or keys. Skips (never fails) when no migrated Postgres is available.
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
        pytest.skip("No DATABASE_URL; discover tests need a live Postgres")
    from sqlalchemy import text

    from app.db.session import SessionLocal, engine, set_tenant

    try:
        with engine.connect() as conn:
            ok = conn.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name='prospect'")
            ).first()
        if ok is None:
            pytest.skip("migrations not applied through 0007; run `alembic upgrade head`")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({exc!r})")

    tenant = uuid.uuid4()
    db = SessionLocal()
    set_tenant(db, tenant)
    yield db, tenant
    from app.models import EmbeddingChunk, Person, PersonSource, Prospect, UserProfile

    for model in (EmbeddingChunk, PersonSource, Prospect, UserProfile):
        db.query(model).filter(model.tenant_id == tenant).delete()
    db.query(Person).filter(Person.tenant_id == tenant).delete()
    db.commit()
    db.close()


class _FakeProvider:
    """Returns fixed candidates + a deterministic email so no network is touched."""

    def __init__(self, candidates):
        self._candidates = candidates

    def search_people(self, profile, *, limit):
        return self._candidates[:limit]

    def find_email(self, name, company):
        from app.services.providers.base import EmailResult

        slug = name.lower().replace(" ", ".")
        return EmailResult(email=f"{slug}@example.com", confidence=90)


def _patch_externals(monkeypatch):
    from app.config import get_settings
    from app.services import discover, relation

    dim = get_settings().voyage_dim
    vec = [0.0] * dim
    vec[0] = 1.0
    # discover.run_discovery batch-embeds profile + candidate snippets in one call.
    monkeypatch.setattr(discover, "embed_documents", lambda texts: [list(vec) for _ in texts])
    # base.ingest embeds via embed_documents on promotion.
    from app.services.connectors import base as base_mod

    monkeypatch.setattr(base_mod, "embed_documents", lambda texts: [list(vec) for _ in texts])
    monkeypatch.setattr(relation, "synthesize", lambda *a, **k: "Shared alma mater and city.")


def _candidate(name, company="Stripe", location="Pittsburgh, PA", school="Carnegie Mellon University"):
    from app.services.providers.base import PersonCandidate

    return PersonCandidate(
        name=name,
        title="Engineer",
        company=company,
        location=location,
        school=school,
        source_url=f"https://www.linkedin.com/in/{name.lower().replace(' ', '')}",
        snippet=f"{name} — Engineer at {company}",
    )


def _save_profile(db, tenant):
    from app.services import discover
    from app.services.profile_capture import UserProfileData

    return discover.save_profile(
        db,
        tenant,
        UserProfileData(
            display_name="Sam Mathew",
            headline="ML Engineer",
            location="Pittsburgh, PA",
            schools=["Carnegie Mellon University"],
            companies=["Stripe"],
            skills=["rag", "embeddings"],
        ),
    )


def test_run_creates_scored_prospects_and_dedupes(db_tenant, monkeypatch):
    from app.models import Prospect
    from app.services import discover

    db, tenant = db_tenant
    _patch_externals(monkeypatch)
    _save_profile(db, tenant)

    provider = _FakeProvider([_candidate("Jane Doe"), _candidate("John Roe")])
    summary = discover.run_discovery(db, tenant, provider=provider, max_candidates=8)
    assert summary.created == 2
    assert summary.skipped_dupes == 0

    rows = db.query(Prospect).filter(Prospect.tenant_id == tenant).all()
    assert len(rows) == 2
    jane = next(r for r in rows if r.name == "Jane Doe")
    assert jane.email == "jane.doe@example.com"  # provider-sourced
    # Same city + same school → high score (geo 0.30 + school 0.30 at minimum).
    assert jane.score >= 60
    assert jane.score_breakdown["features"]["school"] == 1.0

    # Re-running with the same candidates dedupes (idempotent).
    summary2 = discover.run_discovery(db, tenant, provider=provider, max_candidates=8)
    assert summary2.created == 0
    assert summary2.skipped_dupes == 2
    assert db.query(Prospect).filter(Prospect.tenant_id == tenant).count() == 2


def test_promote_to_network_creates_person(db_tenant, monkeypatch):
    from app.models import Person, PersonSource, Prospect
    from app.services import discover

    db, tenant = db_tenant
    _patch_externals(monkeypatch)
    _save_profile(db, tenant)
    provider = _FakeProvider([_candidate("Jane Doe")])
    discover.run_discovery(db, tenant, provider=provider, max_candidates=8)

    prospect = db.query(Prospect).filter(Prospect.tenant_id == tenant).one()
    person = discover.promote_to_network(db, tenant, prospect.id)

    assert person.display_name == "Jane Doe"
    assert person.primary_email == "jane.doe@example.com"
    src = (
        db.query(PersonSource)
        .filter(PersonSource.tenant_id == tenant, PersonSource.source_type == "contactgen")
        .one()
    )
    assert src.person_id == person.id
    db.refresh(prospect)
    assert prospect.status == "saved"
    assert prospect.promoted_person_id == person.id


def test_prospects_are_tenant_isolated(db_tenant, monkeypatch):
    """A second tenant sees none of the first tenant's prospects (RLS)."""
    from app.db.session import SessionLocal, set_tenant
    from app.models import Prospect
    from app.services import discover

    db, tenant = db_tenant
    _patch_externals(monkeypatch)
    _save_profile(db, tenant)
    discover.run_discovery(db, tenant, provider=_FakeProvider([_candidate("Jane Doe")]), max_candidates=8)

    other = uuid.uuid4()
    db2 = SessionLocal()
    set_tenant(db2, other)
    try:
        assert db2.query(Prospect).filter(Prospect.tenant_id == tenant).count() == 0
    finally:
        db2.close()
