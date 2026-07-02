"""Discovery provider interface + shared dataclasses/errors.

Kept dependency-light (no httpx import here) so it's cheap to import in tests and to
build fake providers against. The concrete Brave/Hunter clients live alongside.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # avoid a runtime import cycle (profile_capture imports httpx)
    from app.services.profile_capture import UserProfileData


class ProviderError(Exception):
    """Non-transient provider failure (bad key, malformed request). Surfaced as 502."""


class TransientProviderError(ProviderError):
    """Retryable provider failure (429 / 5xx / network). Retried via backoff."""


@dataclass
class PersonCandidate:
    name: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    school: str | None = None
    source_url: str | None = None
    snippet: str = ""  # text used for interest-similarity embedding
    phone: str | None = None  # only if the discovery result carried one
    raw: dict = field(default_factory=dict)


@dataclass
class EmailResult:
    email: str | None
    confidence: int | None = None
    raw: dict = field(default_factory=dict)


class DiscoveryProvider(Protocol):
    """Turns a user profile into contactable candidates. Implementations must never
    fabricate contact info — email/phone come only from the underlying provider."""

    def search_people(self, profile: "UserProfileData", *, limit: int) -> list[PersonCandidate]:
        ...

    def find_email(self, name: str, company: str | None) -> EmailResult:
        ...
