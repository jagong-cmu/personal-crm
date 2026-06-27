"""RAG eval harness (T10).

Three assertions over the labeled dataset (tests/eval/dataset.py):

  1. INTENT ACCURACY  — classify_intent routes each question to the right path. PURE:
     no DB, no API, always runs. CI gate: accuracy >= 0.9.
  2. RECALL@K         — retrieval surfaces the expected people. Runs against a live
     Postgres seeded with a fixture network and a deterministic offline embedder
     (no Voyage / no Anthropic). Skips when no DB. CI gate: mean recall >= 0.8.
  3. CITATION GROUNDING — every returned citation maps to a REAL seeded person; a
     citation for a non-existent person is a hallucination. CI gate: zero ungrounded.

Run the DB-backed parts:
    docker compose up -d db && alembic upgrade head
    DATABASE_URL=postgresql+psycopg://... pytest tests/eval -q
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import rag
from app.services.rag import classify_intent
from tests.eval.dataset import CASES, INTERACTIONS, NETWORK, fake_embed

# --------------------------------------------------------------------------------------
# 1. Intent accuracy — pure, always runs
# --------------------------------------------------------------------------------------


def test_intent_accuracy():
    correct = 0
    for case in CASES:
        intent, slots = classify_intent(case["q"])
        if intent == case["intent"]:
            correct += 1
            # When an intent is matched, its extracted slots must match the labels.
            for k, v in case["slots"].items():
                assert slots.get(k, "").lower() == v.lower(), f"{case['q']!r}: slot {k}"
    accuracy = correct / len(CASES)
    assert accuracy >= 0.9, f"intent accuracy {accuracy:.2f} < 0.90"


# --------------------------------------------------------------------------------------
# DB-backed fixture: seed the fixture network with offline embeddings
# --------------------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL")
if not _DB_URL:
    try:  # pragma: no cover
        from app.config import get_settings as _gs

        _DB_URL = _gs().database_url
    except Exception:  # noqa: BLE001
        _DB_URL = None


@pytest.fixture(scope="module")
def seeded(request):
    if not _DB_URL:
        pytest.skip("No DATABASE_URL; eval recall/grounding need a live Postgres")
    try:
        from sqlalchemy import text

        from app.config import get_settings
        from app.db.session import SessionLocal, engine, set_tenant
        from app.models import EmbeddingChunk, Interaction, Person
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"app/DB not importable ({exc!r})")

    # Confirm migrations are applied (RLS policy present); else skip rather than fail.
    try:
        with engine.connect() as conn:
            ok = conn.execute(
                text(
                    "SELECT 1 FROM pg_policies WHERE tablename='people' "
                    "AND policyname='tenant_isolation'"
                )
            ).first()
        if ok is None:
            pytest.skip("migrations not applied; run `alembic upgrade head`")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({exc!r})")

    dim = get_settings().voyage_dim
    tenant = uuid.uuid4()
    db = SessionLocal()
    set_tenant(db, tenant)

    key_to_email = {}
    email_to_key = {}
    people = {}
    for key, name, email, company, title, body in NETWORK:
        p = Person(
            tenant_id=tenant, display_name=name, primary_email=email,
            company=company, title=title,
        )
        db.add(p)
        db.flush()
        people[key] = p
        key_to_email[key] = email
        email_to_key[email] = key
        db.add(
            EmbeddingChunk(
                tenant_id=tenant, person_id=p.id, source_type="contacts",
                source_record_id=f"seed:{key}", chunk_text=body,
                content_hash=f"h:{key}", embedding=fake_embed(body, dim),
            )
        )
    for pkey, stype, itype, days_ago, subject in INTERACTIONS:
        db.add(
            Interaction(
                tenant_id=tenant, person_id=people[pkey].id, source_type=stype,
                interaction_type=itype,
                occurred_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                subject=subject, source_record_id=f"seed:{pkey}:{itype}:{days_ago}",
            )
        )
    db.commit()

    # Route semantic retrieval through the SAME offline embedder used to seed vectors.
    orig_embed = rag.embed_query
    rag.embed_query = lambda q: fake_embed(q, dim)

    def _cleanup():
        rag.embed_query = orig_embed
        for model in (Interaction, EmbeddingChunk, Person):
            db.query(model).filter(model.tenant_id == tenant).delete()
        db.commit()
        db.close()

    request.addfinalizer(_cleanup)
    return {"db": db, "tenant": tenant, "email_to_key": email_to_key}


def _retrieve_keys(seeded, case) -> set[str]:
    intent, slots = classify_intent(case["q"])
    if intent == "ambiguous":
        intent = "hybrid"  # mirror runtime fallback without calling Claude
    citations = rag._retrieve(seeded["db"], seeded["tenant"], intent, slots, case["q"])
    return {seeded["email_to_key"][c.email] for c in citations if c.email in seeded["email_to_key"]}


# --------------------------------------------------------------------------------------
# 2. Recall@k
# --------------------------------------------------------------------------------------


def test_recall_at_k(seeded):
    recalls = []
    for case in CASES:
        relevant = case["relevant"]
        if not relevant:  # None (intent-only) or empty (zero-result) handled elsewhere
            continue
        got = _retrieve_keys(seeded, case)
        recalls.append(len(got & relevant) / len(relevant))
    assert recalls, "no recall-labeled cases ran"
    mean_recall = sum(recalls) / len(recalls)
    assert mean_recall >= 0.8, f"mean recall@k {mean_recall:.2f} < 0.80"


def test_zero_result_cases_return_nothing(seeded):
    for case in CASES:
        if case["relevant"] != set():
            continue
        got = _retrieve_keys(seeded, case)
        assert not got, f"{case['q']!r} should match nobody, got {got}"


# --------------------------------------------------------------------------------------
# 3. Citation grounding (hallucination gate)
# --------------------------------------------------------------------------------------


def test_citation_grounding(seeded):
    seeded_emails = set(seeded["email_to_key"])
    ungrounded = []
    for case in CASES:
        intent, slots = classify_intent(case["q"])
        if intent == "ambiguous":
            intent = "hybrid"
        citations = rag._retrieve(seeded["db"], seeded["tenant"], intent, slots, case["q"])
        for c in citations:
            if c.email and c.email not in seeded_emails:
                ungrounded.append((case["q"], c.email))
    assert not ungrounded, f"hallucinated citations: {ungrounded}"
