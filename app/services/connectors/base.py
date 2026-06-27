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
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmbeddingChunk, Interaction, Person, PersonSource, SyncError, SyncState
from app.services import entity_resolution
from app.services.embedding import content_hash, embed_documents

#: Separator joining a source record's base id (gmail message id / calendar event id)
#: to a per-participant key. One message/event fans out to one row per participant; the
#: shared base id prefix lets deletions reconcile every participant row at once.
KEY_SEP = "#"


def participant_key(base_id: str, participant: str) -> str:
    """Build the per-(record, participant) source_record_id. See ``apply_deletions``."""
    return f"{base_id}{KEY_SEP}{participant}"


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
    errors: int = 0  # T7: records routed to the sync_errors dead-letter table
    deletions: int = 0  # T8: source records reconciled as deletes this sync


def _normalize_email(email: str | None) -> str | None:
    return email.strip().lower() if email else None


def resolve(db: Session, tenant_id, rec: NormalizedRecord) -> tuple[Person | None, bool]:
    """Return (person, created), delegating to the T4 entity-resolution engine.

    Follows the alias-first / exact-email / non-human-drop / provisional / fuzzy order.
    A dropped (non-human/list) record returns (None, False); the ingest loop skips it.
    The full ResolutionResult (confidence, needs_review, provisional) is available via
    ``entity_resolution.resolve`` for callers that need it.
    """
    result = entity_resolution.resolve(db, tenant_id, rec)
    return result.person, result.created


def _upsert_source(
    db: Session,
    tenant_id,
    person: Person,
    rec: NormalizedRecord,
    matched_confidence: float | None = None,
) -> None:
    # matched_confidence: the fuzzy(name+company) score when this binding came from a
    # fuzzy merge; None for exact-email / alias / provisional binds (T4). Persisted so
    # the manual-review queue (T14) can rank weak matches.
    stmt = (
        pg_insert(PersonSource)
        .values(
            tenant_id=tenant_id,
            person_id=person.id,
            source_type=rec.source_type,
            source_record_id=rec.source_record_id,
            raw_data=rec.raw,
            matched_confidence=matched_confidence,
        )
        .on_conflict_do_update(
            constraint="uq_person_source",
            set_={
                "raw_data": rec.raw,
                "person_id": person.id,
                "matched_confidence": matched_confidence,
            },
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


def _short_reason(exc: Exception, limit: int = 300) -> str:
    """Privacy-safe failure summary: exception type + truncated message ONLY.

    We deliberately derive the reason from the exception, never from the record
    body/raw/email, so no PII or token payload can leak into sync_errors.
    """
    msg = str(exc).replace("\n", " ").strip()
    if len(msg) > limit:
        msg = msg[:limit] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _record_sync_error(
    db: Session, tenant_id, rec: NormalizedRecord, stage: str, exc: Exception
) -> None:
    """Write a dead-letter row. Stores ONLY source ids + a short reason — never the body."""
    db.add(
        SyncError(
            tenant_id=tenant_id,
            source_type=rec.source_type,
            source_record_id=rec.source_record_id,  # id only
            stage=stage,
            reason=_short_reason(exc),
        )
    )
    db.flush()


def ingest(db: Session, tenant_id, records: list[NormalizedRecord]) -> IngestResult:
    result = IngestResult()
    pending: list[tuple[Person, NormalizedRecord]] = []
    for rec in records:
        # Per-record isolation: a SAVEPOINT lets one bad record roll back without
        # discarding the records already handled in this sync.
        stage = "resolve"
        try:
            with db.begin_nested():
                # Call the engine directly (not the resolve() wrapper) so the fuzzy
                # match confidence reaches _upsert_source for the review queue (T14).
                res = entity_resolution.resolve(db, tenant_id, rec)
                if res.person is None:
                    # Non-human / list record intentionally dropped by resolution
                    # (T4). Not a failure — skip it without a dead-letter row.
                    continue
                stage = "upsert"
                _upsert_source(db, tenant_id, res.person, rec, res.confidence)
        except Exception as exc:  # noqa: BLE001 - isolate any per-record failure
            result.errors += 1
            _record_sync_error(db, tenant_id, rec, stage, exc)
            continue
        result.people_created += int(res.created)
        result.people_matched += int(not res.created)
        result.sources_upserted += 1
        pending.append((res.person, rec))

    # Embedding: Voyage already retries with backoff; if it still fails, dead-letter
    # the affected records instead of crashing the whole sync.
    try:
        with db.begin_nested():
            embedded, skipped = _embed_pending(db, tenant_id, pending)
        result.chunks_embedded = embedded
        result.chunks_skipped = skipped
    except Exception as exc:  # noqa: BLE001 - embed batch failed after backoff
        for _, rec in pending:
            result.errors += 1
            _record_sync_error(db, tenant_id, rec, "embed", exc)

    db.commit()
    return result


# --------------------------------------------------------------------------------------
# Interactions (timeline rows for the structured query path)
# --------------------------------------------------------------------------------------


def record_interaction(
    db: Session,
    tenant_id,
    *,
    person: Person,
    source_type: str,
    source_record_id: str,
    interaction_type: str,
    occurred_at: datetime | None,
    subject: str | None,
    external_id: str,
) -> None:
    """Upsert one timeline interaction (email/meeting) linking the user to ``person``.

    Idempotent on (tenant, source_type, source_record_id) — re-syncs update in place,
    never duplicate. ``external_id`` (the gmail message / calendar event id, no
    participant suffix) is stored in metadata so ``apply_deletions`` can reconcile every
    participant row of a deleted message/event. A re-appearing record clears any prior
    soft-delete (deleted_at -> NULL).
    """
    stmt = (
        pg_insert(Interaction)
        .values(
            tenant_id=tenant_id,
            person_id=person.id,
            source_type=source_type,
            source_record_id=source_record_id,
            interaction_type=interaction_type,
            occurred_at=occurred_at,
            subject=subject,
            metadata={"external_id": external_id},
            deleted_at=None,
        )
        .on_conflict_do_update(
            constraint="uq_interaction_source",
            set_={
                "person_id": person.id,
                "interaction_type": interaction_type,
                "occurred_at": occurred_at,
                "subject": subject,
                "metadata": {"external_id": external_id},
                "deleted_at": None,
            },
        )
    )
    db.execute(stmt)


# --------------------------------------------------------------------------------------
# Deletions (decision: deletions)
# --------------------------------------------------------------------------------------


def apply_deletions(
    db: Session, tenant_id, source_type: str, base_ids: list[str]
) -> int:
    """Reconcile deleted source records: soft-delete interactions + drop their embeddings.

    ``base_ids`` are message/event ids WITHOUT the participant suffix. For each we:
      * soft-delete every interaction row for that record (deleted_at = now), and
      * hard-delete its embedding chunks so they stop surfacing in retrieval.

    Per-participant rows share the ``base_id#`` source_record_id prefix (see
    ``participant_key``), so a prefix match reconciles all of them. Returns the number
    of base ids processed. Retrieval already filters ``deleted_at IS NULL``.
    """
    if not base_ids:
        return 0
    now = datetime.now(timezone.utc)
    for base_id in base_ids:
        prefix = f"{base_id}{KEY_SEP}%"
        db.execute(
            update(Interaction)
            .where(
                Interaction.tenant_id == tenant_id,
                Interaction.source_type == source_type,
                Interaction.source_record_id.like(prefix),
            )
            .values(deleted_at=now)
        )
        db.execute(
            delete(EmbeddingChunk).where(
                EmbeddingChunk.tenant_id == tenant_id,
                EmbeddingChunk.source_type == source_type,
                EmbeddingChunk.source_record_id.like(prefix),
            )
        )
    db.commit()
    return len(base_ids)


# --------------------------------------------------------------------------------------
# Incremental-sync cursors (T13)
# --------------------------------------------------------------------------------------


def get_cursor(db: Session, tenant_id, source_type: str) -> str | None:
    """Return the stored incremental cursor (historyId / syncToken) or None."""
    return db.scalar(
        select(SyncState.cursor).where(
            SyncState.tenant_id == tenant_id, SyncState.source_type == source_type
        )
    )


def set_cursor(db: Session, tenant_id, source_type: str, cursor: str | None) -> None:
    """Persist the incremental cursor for a source. Upsert on (tenant, source)."""
    stmt = (
        pg_insert(SyncState)
        .values(
            tenant_id=tenant_id,
            source_type=source_type,
            cursor=cursor,
            last_synced_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            constraint="uq_sync_state_source",
            set_={"cursor": cursor, "last_synced_at": datetime.now(timezone.utc)},
        )
    )
    db.execute(stmt)
    db.commit()
