"""pgvector HNSW index on embedding_chunks.embedding (T17)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-26

Slice 0 ran exact (flat) cosine scans — fine at small scale, O(n) per query. This
adds an HNSW approximate-nearest-neighbour index so retrieval stays fast as the
embedding count grows.

OPERATOR CLASS
--------------
We embed with cosine distance (``EmbeddingChunk.embedding.cosine_distance`` /
``input_type`` asymmetry in embedding.py), so the index uses ``vector_cosine_ops``.
The query's ``ORDER BY embedding <=> :q`` (cosine) can then use this index.

TENANT PREDICATE INTERACTION (decision A1 / T17 note)
-----------------------------------------------------
Queries filter by tenant (RLS + an explicit ``tenant_id =`` WHERE clause). pgvector
applies the ANN index first, then post-filters surviving rows by the tenant predicate.
At single-tenant dev scale this is a non-issue. If/when many tenants share the table
and recall suffers from post-filtering, the follow-up is a partitioned or partial
index per tenant — out of scope here, flagged in PLAN.md.

PARAMETERS
----------
``m=16, ef_construction=64`` are pgvector's defaults — good general-purpose recall
for networks up to ~1M vectors. Build cost is paid once here.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_embedding_chunks_hnsw "
        "ON embedding_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embedding_chunks_hnsw")
