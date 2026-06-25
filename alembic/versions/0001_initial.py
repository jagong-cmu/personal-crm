"""initial schema (Slice 0)

Revision ID: 0001
Revises:
Create Date: 2026-06-25

Creates pgvector + the six core tables with tenant_id everywhere and the natural-key
unique constraints (decision A2). RLS policies and the HNSW index are deferred to the
hardening slice. The embedding dimension is read from app settings (decision A3).
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_DIM = get_settings().voyage_dim
_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "people",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("primary_email", sa.Text()),
        sa.Column("company", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, server_default=sa.text("now()")),
    )
    op.create_index("ix_people_tenant_id", "people", ["tenant_id"])
    op.create_index("ix_people_primary_email", "people", ["primary_email"])

    op.create_table(
        "person_sources",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("person_id", _UUID, sa.ForeignKey("people.id"), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("matched_confidence", sa.Float()),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "tenant_id", "source_type", "source_record_id", name="uq_person_source"
        ),
    )
    op.create_index("ix_person_sources_tenant_id", "person_sources", ["tenant_id"])

    op.create_table(
        "organizations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("domain", sa.Text()),
    )
    op.create_index("ix_organizations_tenant_id", "organizations", ["tenant_id"])
    op.create_index("ix_organizations_domain", "organizations", ["domain"])

    op.create_table(
        "interactions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("person_id", _UUID, sa.ForeignKey("people.id"), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("interaction_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", _TS),
        sa.Column("subject", sa.Text()),
        sa.Column("source_record_id", sa.Text()),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("deleted_at", _TS),
        sa.UniqueConstraint(
            "tenant_id", "source_type", "source_record_id", name="uq_interaction_source"
        ),
    )
    op.create_index("ix_interactions_tenant_id", "interactions", ["tenant_id"])

    op.create_table(
        "embedding_chunks",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("person_id", _UUID, sa.ForeignKey("people.id"), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_DIM)),
        sa.Column("created_at", _TS, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "tenant_id", "source_record_id", "content_hash", name="uq_embedding_content"
        ),
    )
    op.create_index("ix_embedding_chunks_tenant_id", "embedding_chunks", ["tenant_id"])

    op.create_table(
        "oauth_credentials",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_access_token", sa.Text()),
        sa.Column("encrypted_refresh_token", sa.Text()),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("expires_at", _TS),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_oauth_tenant_provider"),
    )
    op.create_index("ix_oauth_credentials_tenant_id", "oauth_credentials", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("oauth_credentials")
    op.drop_table("embedding_chunks")
    op.drop_table("interactions")
    op.drop_table("organizations")
    op.drop_table("person_sources")
    op.drop_table("people")
