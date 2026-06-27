"""Re-embedding backfill (T18).

When VOYAGE_MODEL or VOYAGE_DIM changes, every stored vector is stale — it was produced
by a different model and/or has the wrong dimensionality. This job re-embeds the stored
``chunk_text`` of every embedding_chunk with the CURRENT model/dim and rewrites the
vector in place.

PREREQUISITE for a dimension change: the embedding column type must already match the new
VOYAGE_DIM (an Alembic migration altering ``vector(N)`` + the startup assertion in
app/main.py). This job only refreshes the vector values; it does not alter the schema.

Run: python -m app.workers.backfill
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal, set_tenant
from app.models import EmbeddingChunk
from app.services.embedding import embed_documents

logger = logging.getLogger(__name__)
_BATCH = 128


def run_backfill(tenant_id=None) -> int:
    """Re-embed every chunk for the tenant with the current model. Returns count updated."""
    tenant_id = tenant_id or get_settings().tenant_uuid
    db = SessionLocal()
    updated = 0
    try:
        set_tenant(db, tenant_id)
        chunks = db.scalars(
            select(EmbeddingChunk).where(EmbeddingChunk.tenant_id == tenant_id)
        ).all()
        for i in range(0, len(chunks), _BATCH):
            batch = chunks[i : i + _BATCH]
            vectors = embed_documents([c.chunk_text for c in batch])
            for chunk, vec in zip(batch, vectors):
                chunk.embedding = vec
                updated += 1
            db.commit()
            logger.info("re-embedded %d/%d", updated, len(chunks))
        return updated
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    n = run_backfill()
    print(f"re-embedded {n} chunks")
