"""Offline unit tests for the T4 entity-resolution engine.

The pure functions (normalize_email, is_non_human, name_company_similarity) need no
database and run fully offline. The DB-backed ``resolve`` paths are exercised only when
a Postgres TEST_DATABASE_URL is provided, otherwise skipped.
"""
from __future__ import annotations

import os

import pytest

from app.services.entity_resolution import (
    FUZZY_MERGE_THRESHOLD,
    ResolutionResult,
    is_non_human,
    name_company_similarity,
    normalize_email,
)

# --------------------------------------------------------------------------------------
# normalize_email
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Jane.Doe@Example.COM ", "jane.doe@example.com"),
        ("a@b.com", "a@b.com"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


# --------------------------------------------------------------------------------------
# is_non_human  (non-human / list filter)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "no-reply@example.com",
        "noreply@example.com",
        "do-not-reply@shop.com",
        "donotreply@shop.com",
        "calendar-notification@google.com",
        "calendar@group.calendar.google.com",
        "mailer-daemon@mx.example.com",
        "postmaster@example.com",
        "bounce@news.example.com",
        "list-bounces@lists.example.org",
        "dev-list-request@lists.example.org",
        "marketing+unsubscribe@brand.com",
        "notifications@github.com",
        "anything@bounce.example.com",
    ],
)
def test_is_non_human_true(email):
    assert is_non_human(email, None) is True


@pytest.mark.parametrize(
    "email,name",
    [
        ("jane.doe@example.com", "Jane Doe"),
        ("bob@acme.com", "Bob Smith"),
        ("a.replyguy@personal.org", "Reply Guy"),  # 'reply' substring but not no-reply
        ("calendarsmith@example.com", "Cal Smith"),  # 'calendar' not a prefix/exact
    ],
)
def test_is_non_human_false_for_real_people(email, name):
    assert is_non_human(email, name) is False


def test_is_non_human_no_email_is_not_dropped():
    # Name-only records are NOT non-human (they may be real people).
    assert is_non_human(None, "Jane Doe") is False


def test_is_non_human_drops_users_own_aliases():
    own = {"me@self.com", "Me+work@Self.com"}
    assert is_non_human("me@self.com", "Me", own_emails=own) is True
    assert is_non_human("me+work@self.com", "Me", own_emails=own) is True
    assert is_non_human("someone@self.com", "Else", own_emails=own) is False


# --------------------------------------------------------------------------------------
# name_company_similarity  (company-gated fuzzy)
# --------------------------------------------------------------------------------------


def test_similarity_identical_name_and_company_is_one():
    score = name_company_similarity("Jane Doe", "Acme Inc", "Jane Doe", "Acme Inc.")
    assert score == pytest.approx(1.0)
    assert score >= FUZZY_MERGE_THRESHOLD  # above the boundary


def test_similarity_company_gating_returns_zero_without_company():
    # Fuzzy fires ONLY when BOTH records carry a company.
    assert name_company_similarity("Jane Doe", None, "Jane Doe", "Acme") == 0.0
    assert name_company_similarity("Jane Doe", "Acme", "Jane Doe", None) == 0.0
    assert name_company_similarity("Jane Doe", None, "Jane Doe", None) == 0.0


def test_similarity_same_name_different_company_below_threshold():
    # Same person name but clearly different employer must NOT auto-merge.
    score = name_company_similarity("Jane Doe", "Acme Inc", "Jane Doe", "Globex")
    assert score < FUZZY_MERGE_THRESHOLD


def test_similarity_different_name_same_company_below_threshold():
    # Two coworkers must never collapse into one person.
    score = name_company_similarity("Jane Doe", "Acme", "John Smith", "Acme")
    assert score < FUZZY_MERGE_THRESHOLD


def test_similarity_boundary_both_sides():
    # Just-above: nickname variant at the same company crosses the 0.85 line.
    above = name_company_similarity("Bob Jones", "Acme Corp", "Robert Jones", "Acme Corporation")
    assert above >= FUZZY_MERGE_THRESHOLD
    # Just-below: same name, different company stays under.
    below = name_company_similarity("Jane Doe", "Acme Inc", "Jane Doe", "Globex")
    assert below < FUZZY_MERGE_THRESHOLD
    assert above > below


def test_similarity_company_suffix_normalization():
    # Legal suffixes are stripped, so "Acme" == "Acme Inc" == "Acme, LLC".
    assert name_company_similarity("Jane Doe", "Acme", "Jane Doe", "Acme Inc") == pytest.approx(1.0)
    assert name_company_similarity("Jane Doe", "Acme", "Jane Doe", "Acme, LLC") == pytest.approx(1.0)


def test_resolution_result_defaults():
    r = ResolutionResult(person=None, created=False)
    assert r.confidence is None
    assert r.needs_review is False
    assert r.provisional is False
    assert r.dropped is False


# --------------------------------------------------------------------------------------
# DB-backed resolution  (skipped without a real Postgres + pgvector)
# --------------------------------------------------------------------------------------

_TEST_DB = os.environ.get("TEST_DATABASE_URL")
_db_required = pytest.mark.skipif(_TEST_DB is None, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.session import Base
    import app.models  # noqa: F401  (register tables)

    engine = create_engine(_TEST_DB)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
        s.rollback()


def _rec(**kw):
    from app.services.connectors.base import NormalizedRecord

    base = dict(
        source_type="contacts",
        source_record_id="people/1",
        display_name=None,
        primary_email=None,
    )
    base.update(kw)
    return NormalizedRecord(**base)


@_db_required
def test_resolve_provisional_email_no_company(db_session):
    import uuid

    from app.services.entity_resolution import resolve

    t = uuid.uuid4()
    rec = _rec(display_name="Jane", primary_email="jane@example.com")
    r = resolve(db_session, t, rec)
    assert r.created is True
    assert r.provisional is True
    assert r.person.primary_email == "jane@example.com"


@_db_required
def test_resolve_exact_email_binds(db_session):
    import uuid

    from app.services.entity_resolution import resolve

    t = uuid.uuid4()
    first = resolve(db_session, t, _rec(source_record_id="a", primary_email="x@y.com"))
    db_session.flush()
    second = resolve(db_session, t, _rec(source_record_id="b", primary_email="X@Y.com"))
    assert second.created is False
    assert second.person.id == first.person.id


@_db_required
def test_resolve_non_human_dropped(db_session):
    import uuid

    from app.services.entity_resolution import resolve

    t = uuid.uuid4()
    r = resolve(db_session, t, _rec(primary_email="no-reply@example.com"))
    assert r.dropped is True
    assert r.person is None


@_db_required
def test_resolve_alias_first(db_session):
    import uuid

    from app.models import Person, PersonAlias
    from app.services.entity_resolution import resolve

    t = uuid.uuid4()
    p = Person(tenant_id=t, display_name="Canonical", primary_email="c@x.com")
    db_session.add(p)
    db_session.flush()
    db_session.add(
        PersonAlias(
            tenant_id=t,
            source_type="contacts",
            source_record_id="people/aliased",
            person_id=p.id,
            decided_by="manual",
        )
    )
    db_session.flush()
    # Different email entirely, but the alias forces the bind.
    r = resolve(db_session, t, _rec(source_record_id="people/aliased", primary_email="other@z.com"))
    assert r.created is False
    assert r.person.id == p.id


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
