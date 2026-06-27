"""Voyage AI embedding wrapper.

Decision A3: model + dim come from settings, passed explicitly on every call.
Decision A2: content_hash lets the ingest path skip re-embedding unchanged text.
Voyage's input_type asymmetry — "document" at ingest, "query" at search time.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, TypeVar

import voyageai
import voyageai.error as voyage_error

from app.config import get_settings

_client: voyageai.Client | None = None
_BATCH = 128

# T7: exponential backoff for transient Voyage failures. Tunable module constants.
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY = 0.5  # seconds; delay = base * 2**attempt
_RETRY_MAX_DELAY = 30.0  # cap per-sleep
# Only transient / rate-limit errors are retried; auth/bad-request errors re-raise at once.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    voyage_error.RateLimitError,
    voyage_error.ServerError,
    voyage_error.ServiceUnavailableError,
    voyage_error.APIConnectionError,
)

_T = TypeVar("_T")


def _voyage() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(api_key=get_settings().voyage_api_key)
    return _client


def _call_with_backoff(
    fn: Callable[[], _T],
    *,
    max_attempts: int = _RETRY_MAX_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY,
    max_delay: float = _RETRY_MAX_DELAY,
    transient: tuple[type[BaseException], ...] = _TRANSIENT_ERRORS,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Call ``fn`` with exponential backoff on transient errors.

    Retries up to ``max_attempts`` times, sleeping ``min(base_delay * 2**attempt,
    max_delay)`` between tries. Re-raises the last error after the cap is hit, and
    re-raises non-transient errors immediately. ``fn`` and ``sleep`` are injectable
    so this is unit-testable offline with a fake embed callable.
    """
    for attempt in range(max_attempts):
        try:
            return fn()
        except transient:
            if attempt == max_attempts - 1:
                raise
            sleep(min(base_delay * (2 ** attempt), max_delay))
    raise AssertionError("unreachable")  # pragma: no cover


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch-embed ingest-time text. Order preserved."""
    s = get_settings()
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i : i + _BATCH]
        result = _call_with_backoff(
            lambda batch=batch: _voyage().embed(
                batch, model=s.voyage_model, input_type="document", output_dimension=s.voyage_dim
            )
        )
        out.extend(result.embeddings)
    return out


def embed_query(text: str) -> list[float]:
    s = get_settings()
    result = _voyage().embed(
        [text], model=s.voyage_model, input_type="query", output_dimension=s.voyage_dim
    )
    return result.embeddings[0]
