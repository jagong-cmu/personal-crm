"""Discovery providers for the Discover (contact generator) feature.

A ``DiscoveryProvider`` turns the user's profile into candidate people (web discovery)
and looks up their contact info. The only real provider composes Brave Search (discovery)
+ Hunter.io (email). Contact info is ALWAYS provider-sourced, never LLM-fabricated.

``get_provider(settings)`` returns a configured provider, or None when keys are missing
(the feature-disabled signal used by the API layer).
"""
from __future__ import annotations

from app.services.providers.base import (
    DiscoveryProvider,
    EmailResult,
    PersonCandidate,
    ProviderError,
    TransientProviderError,
)
from app.services.providers.real import get_provider

__all__ = [
    "DiscoveryProvider",
    "EmailResult",
    "PersonCandidate",
    "ProviderError",
    "TransientProviderError",
    "get_provider",
]
