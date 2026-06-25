"""Shared ingest tail (decision C1: amended).

Every source normalizes its records to NormalizedRecord, then calls ingest().
The sync/parse LOOP stays in each connector; only this tail is shared:

    normalize ─▶ resolve ─▶ upsert person_source ─▶ embed (batched, hash-skipped)

Slice 0 resolution is exact-email-or-new. Alias-first lookup, fuzzy(name+company),
provisional/non-human filtering (decisions A4 / merge persistence) land in the
entity-resolution hardening slice — this is the seam they plug into.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmbeddingChunk, Person, PersonSource
from app.services.embedding import content_hash, embed_documents


@dataclass
class NormalizedRecord:
    source_type: str
    source_record_id: str
    display_name: str | None
    primary_email: str | None
    company: str | None = None
    title: str | None = None
    text: str = ""  # what gets embedded
    raw: dict = field(default_factory=dict)


@dataclass
class IngestResult:
    people_created: int = 0
    people_matched: int = 0
    sources_upserted: int = 0
    chunks_embedded: int = 0
    chunks_skipped: int = 0


def _normalize_email(email: str | None) -> str | None:
    return email.strip().lower() if email else None


def resolve(db: Session, tenant_id, rec: NormalizedRecord) -> tuple[Person, bool]:
    """Return (person, created). Slice 0: exact normalized-email match, else new."""
    email = _normalize_email(rec.primary_email)
    if email:
        existing = db.scalar(
            select(Person).where(Person.tenant_id == tenant_id, Person.primary_email == email)
        )
        if existing is not None:
            return existing, False

    person = Person(
        tenant_id=tenant_id,
        display_name=rec.display_name,
        primary_email=email,
        company=rec.company,
        title=rec.title,
    )
    db.add(person)
    db.flush()  # assign id
    return person, True


def _upsert_source(db: Session, tenant_id, person: Person, rec: NormalizedRecord) -> None:
    stmt = (
        pg_insert(PersonSource)
        .values(
            tenant_id=tenant_id,
            person_id=person.id,
            source_type=rec.source_type,
            source_record_id=rec.source_record_id,
            raw_data=rec.raw,
            matched_confidence=None,  # exact/null for Slice 0
        )
        .on_conflict_do_update(
            constraint="uq_person_source",
            set_={"raw_data": rec.raw, "person_id": person.id},
        )
    )
    db.execute(stmt)


def _embed_pending(db: Session, tenant_id, pending: list[tuple[Person, NormalizedRecord]]) -> tuple[int, int]:
    """Batch-embed records whose (source_record_id, content_hash) isn't stored yet."""
    to_embed: list[tuple[Person, NormalizedRecord, str]] = []
    skipped = 0
    for person, rec in pending:
        if not rec.text.strip():
            continue
        h = content_hash(rec.text)
        already = db.scalar(
            select(EmbeddingChunk.id).where(
                EmbeddingChunk.tenant_id == tenant_id,
                EmbeddingChunk.source_record_id == rec.source_record_id,
                EmbeddingChunk.content_hash == h,
            )
        )
        if already is not None:
            skipped += 1
            continue
        to_embed.append((person, rec, h))

    if not to_embed:
        return 0, skipped

    vectors = embed_documents([rec.text for _, rec, _ in to_embed])
    for (person, rec, h), vec in zip(to_embed, vectors):
        stmt = (
            pg_insert(EmbeddingChunk)
            .values(
                tenant_id=tenant_id,
                person_id=person.id,
                source_type=rec.source_type,
                source_record_id=rec.source_record_id,
                chunk_text=rec.text,
                content_hash=h,
                embedding=vec,
            )
            .on_conflict_do_nothing(constraint="uq_embedding_content")
        )
        db.execute(stmt)
    return len(to_embed), skipped


def ingest(db: Session, tenant_id, records: list[NormalizedRecord]) -> IngestResult:
    result = IngestResult()
    pending: list[tuple[Person, NormalizedRecord]] = []
    for rec in records:
        person, created = resolve(db, tenant_id, rec)
        result.people_created += int(created)
        result.people_matched += int(not created)
        _upsert_source(db, tenant_id, person, rec)
        result.sources_upserted += 1
        pending.append((person, rec))

    embedded, skipped = _embed_pending(db, tenant_id, pending)
    result.chunks_embedded = embedded
    result.chunks_skipped = skipped
    db.commit()
    return result
