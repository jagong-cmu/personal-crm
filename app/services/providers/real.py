"""Real discovery provider: Brave Search (people discovery) + Hunter.io (email lookup).

Composes the two clients into the ``DiscoveryProvider`` interface and adds transient-error
backoff (reusing the generic helper from the embedding layer). ``get_provider`` returns
None when either key is missing, which the API layer treats as "Discover disabled".
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.config import Settings
from app.services.embedding import _call_with_backoff
from app.services.providers.base import (
    DiscoveryProvider,
    EmailResult,
    PersonCandidate,
    TransientProviderError,
)
from app.services.providers.brave import BraveClient, build_queries, parse_results
from app.services.providers.hunter import HunterClient

if TYPE_CHECKING:
    from app.services.profile_capture import UserProfileData

logger = logging.getLogger(__name__)

#: Retry only our own transient wrapper; ProviderError (bad key) propagates immediately.
_TRANSIENT = (TransientProviderError,)


class RealProvider:
    def __init__(self, search: BraveClient, hunter: HunterClient) -> None:
        self._search = search
        self._hunter = hunter

    def search_people(self, profile: "UserProfileData", *, limit: int) -> list[PersonCandidate]:
        """Run the profile's queries until we have ``limit`` unique candidates."""
        seen: set[str] = set()
        out: list[PersonCandidate] = []
        for query in build_queries(profile):
            if len(out) >= limit:
                break
            payload = _call_with_backoff(
                lambda q=query: self._search.search(q, num=10), transient=_TRANSIENT
            )
            for cand in parse_results(payload):
                key = (cand.source_url or cand.name).lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(cand)
                if len(out) >= limit:
                    break
        return out

    def find_email(self, name: str, company: str | None) -> EmailResult:
        return _call_with_backoff(
            lambda: self._hunter.find_email(name, company), transient=_TRANSIENT
        )


def get_provider(settings: Settings) -> DiscoveryProvider | None:
    """Return a configured RealProvider, or None when either API key is absent."""
    if not settings.discovery_enabled:
        return None
    return RealProvider(
        BraveClient(settings.brave_api_key),
        HunterClient(settings.hunter_api_key),
    )
