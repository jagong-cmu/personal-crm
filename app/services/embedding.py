"""Voyage AI embedding wrapper.

Decision A3: model + dim come from settings, passed explicitly on every call.
Decision A2: content_hash lets the ingest path skip re-embedding unchanged text.
Voyage's input_type asymmetry — "document" at ingest, "query" at search time.
"""
from __future__ import annotations

import hashlib

import voyageai

from app.config import get_settings

_client: voyageai.Client | None = None
_BATCH = 128


def _voyage() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(api_key=get_settings().voyage_api_key)
    return _client


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch-embed ingest-time text. Order preserved."""
    s = get_settings()
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i : i + _BATCH]
        result = _voyage().embed(
            batch, model=s.voyage_model, input_type="document", output_dimension=s.voyage_dim
        )
        out.extend(result.embeddings)
    return out


def embed_query(text: str) -> list[float]:
    s = get_settings()
    result = _voyage().embed(
        [text], model=s.voyage_model, input_type="query", output_dimension=s.voyage_dim
    )
    return result.embeddings[0]
