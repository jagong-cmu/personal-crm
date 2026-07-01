"""Shared Anthropic client (lazy global).

Extracted so both the RAG synthesis path (app/services/rag.py) and the Discover
relation-synthesis path (app/services/relation.py) share one client instance instead
of each holding a private global. The API key comes from settings; the model is chosen
per call site via ``get_settings().anthropic_model``.
"""
from __future__ import annotations

import anthropic

from app.config import get_settings

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return _client
