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


__all__ = [
    "Person",
    "PersonSource",
    "PersonAlias",
    "Organization",
    "Interaction",
    "EmbeddingChunk",
    "OAuthCredential",
    "SyncError",
]
