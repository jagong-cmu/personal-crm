"""ORM models. tenant_id on every table from day one (multi-tenant-ready).

  ingest pipeline (shared tail, decision C1):
    raw record ──normalize──▶ {display_name, primary_email, company, title, text}
        │
        ├─ resolve ──▶ people (canonical)        ◀── exact email match | new (Slice 0)
        ├─ upsert  ──▶ person_sources            UNIQUE(tenant, source_type, source_record_id)
        └─ embed   ──▶ embedding_chunks          UNIQUE(tenant, source_record_id, content_hash)

interactions / organizations are created now but exercised in later slices.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db.session import Base

_DIM = get_settings().voyage_dim


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Person(Base):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    primary_email: Mapped[str | None] = mapped_column(Text, index=True)
    company: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    # Manual-merge tombstone (T14): when set, this person was merged INTO another and is
    # hidden from listings/retrieval. Un-merge clears it back to NULL.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list["PersonSource"]] = relationship(back_populates="person")


class PersonSource(Base):
    __tablename__ = "person_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", "source_record_id", name="uq_person_source"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # contacts|gmail|calendar|linkedin
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    matched_confidence: Mapped[float | None] = mapped_column(Float)  # null = exact email match
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    person: Mapped[Person] = relationship(back_populates="sources")


class PersonAlias(Base):
    """Sticky source-record -> person merges (T3). Resolution checks aliases FIRST so a
    manual or confident-auto merge persists across re-syncs. Un-merge = delete the row.
    """

    __tablename__ = "person_aliases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_type", "source_record_id", name="uq_person_alias"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id"), nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)  # 'manual' | 'auto'
    # T14: the loser person this alias moved a source record AWAY from, so un-merge can
    # repoint exactly that record back. NULL for auto/confident-merge aliases.
    merged_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text, index=True)


class Interaction(Base):
    __tablename__ = "interactions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", "source_record_id", name="uq_interaction_source"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    interaction_type: Mapped[str] = mapped_column(Text, nullable=False)  # email_sent|meeting|...
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subject: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # soft-delete (decision: deletions)


class EmbeddingChunk(Base):
    __tablename__ = "embedding_chunks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_record_id", "content_hash", name="uq_embedding_content"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)  # A2: skip re-embed when unchanged
    embedding: Mapped[list[float]] = mapped_column(Vector(_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthCredential(Base):
    __tablename__ = "oauth_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_oauth_tenant_provider"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # google
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncState(Base):
    """Per-source incremental-sync cursor (T13).

    Stores the opaque resumption token each polling source needs so the next sync
    picks up where the last left off instead of re-fetching everything:

      * gmail    -> ``cursor`` holds the last processed Gmail ``historyId``
      * calendar -> ``cursor`` holds the Calendar ``syncToken``
      * contacts -> ``cursor`` holds the People API ``syncToken`` (optional)

    On token invalidation (Gmail ``historyId`` 404 / Calendar ``syncToken`` 410) the
    connector clears the cursor and does a full resync, which reconciles deletions
    (decision: deletions). One row per (tenant, source).
    """

    __tablename__ = "sync_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", name="uq_sync_state_source"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # gmail|calendar|contacts
    cursor: Mapped[str | None] = mapped_column(Text)  # historyId | syncToken | None
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncError(Base):
    """Dead-letter row for a record that failed ingest (T7).

    Privacy: stores ONLY the source record id + a short reason string. NEVER the
    raw record body, tokens, emails, or any PII payload.
    """

    __tablename__ = "sync_errors"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # contacts|gmail|calendar|...
    source_record_id: Mapped[str | None] = mapped_column(Text)  # id only, never the body
    stage: Mapped[str] = mapped_column(Text, nullable=False)  # resolve|upsert|embed
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # short message, no payload/PII
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserProfile(Base):
    """The user's OWN background/interests, powering Discover (contact generator).

    One row per tenant (uq_user_profile_tenant) so save = upsert. Captured from a
    best-effort LinkedIn scrape or the manual import form. No stored embedding — the
    profile vector is recomputed per discovery run (one cheap embed_query call).
    """

    __tablename__ = "user_profile"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_user_profile_tenant"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    schools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    companies: Mapped[list[str]] = mapped_column(JSONB, default=list)
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)  # interests/skills
    about: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)  # scrape/import snapshot
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Prospect(Base):
    """A generated outreach candidate (Discover). Kept SEPARATE from the real network
    (people) until promoted via 'Save to network' (base.ingest, source_type=contactgen).

    Contact fields (email/phone) are provider-sourced ONLY — never LLM-fabricated.
    uq_prospect_dedupe makes re-runs idempotent. score_breakdown carries the transparent
    per-feature scoring (see app/services/scoring.py).
    """

    __tablename__ = "prospect"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_prospect_dedupe"),
        Index("ix_prospect_tenant_status", "tenant_id", "status"),
        Index("ix_prospect_tenant_score", "tenant_id", "score"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)  # from Hunter ONLY
    phone: Mapped[str | None] = mapped_column(Text)  # from provider ONLY
    company: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    school: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)  # Brave search result link
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    score_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)
    relation_summary: Mapped[str | None] = mapped_column(Text)  # None if LLM unavailable
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")  # new|saved|dismissed
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    promoted_person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id"))
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)  # provider payloads (audit)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "Person",
    "PersonSource",
    "PersonAlias",
    "Organization",
    "Interaction",
    "EmbeddingChunk",
    "OAuthCredential",
    "SyncState",
    "SyncError",
    "UserProfile",
    "Prospect",
]
