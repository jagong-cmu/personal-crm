"""Brave Search API discovery client — turn the user's profile into candidate people.

``build_queries`` and ``parse_results`` are PURE (unit-testable against canned JSON);
only ``search`` touches the network. We target public LinkedIn profiles via Brave web
search so the result descriptions carry name / title / company / location we can extract
heuristically.

Extraction from web results is inherently noisy — this is the highest-variance piece of
the feature, which is exactly why the parser is pure and fixture-tested.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx

from app.services.providers.base import (
    PersonCandidate,
    ProviderError,
    TransientProviderError,
)

if TYPE_CHECKING:
    from app.services.profile_capture import UserProfileData

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 20.0


def build_queries(profile: "UserProfileData") -> list[str]:
    """Build a few targeted web queries from the profile, most-specific first.

    Combines the strongest signals (school, location, top skill, company) so the top
    results skew toward people who actually overlap with the user. De-duplicated, capped.
    """
    top_skills = profile.skills[:2]
    school = profile.schools[0] if profile.schools else None
    company = profile.companies[0] if profile.companies else None
    loc = profile.location
    site = "site:linkedin.com/in"

    queries: list[str] = []

    def add(*parts: str | None) -> None:
        toks = [p.strip() for p in parts if p and p.strip()]
        if not toks:
            return
        q = " ".join(toks + [site])
        if q not in queries:
            queries.append(q)

    for skill in (top_skills or [None]):
        add(f'"{school}"' if school else None, skill)
        add(company, loc, skill)
        add(loc, skill)
    add(profile.headline)

    return queries[:4] or ([f"{profile.headline} {site}"] if profile.headline else [])


# A location only when it looks like a real place: "City, ST", "City, Country", or a
# "... Area" phrase. Brave descriptions are prose, so a greedy match grabs sentences
# ("Research at Carnegie Mellon as a...") — we require a place-shaped tail or take None.
_LOCATION_HINT = re.compile(
    r"\b([A-Z][A-Za-z.]+(?:\s[A-Z][A-Za-z.]+){0,2}"
    r"(?:,\s*(?:[A-Z]{2}|[A-Z][a-z]+)|\sArea))\b"
)


# Reject date-shaped false positives ("Thursday, June", "Monday, May 5").
_DATE_WORDS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def _extract_location(snippet: str) -> str | None:
    m = _LOCATION_HINT.search(snippet)
    if not m:
        return None
    loc = m.group(1).strip()
    if len(loc) > 40:
        return None
    if any(w in _DATE_WORDS for w in re.findall(r"[A-Za-z]+", loc.lower())):
        return None
    return loc


def _parse_one(result: dict) -> PersonCandidate | None:
    title = result.get("title") or ""
    link = result.get("url") or ""
    snippet = result.get("description") or ""
    if not title:
        return None

    # LinkedIn titles look like "Jane Doe - Staff Engineer - Stripe | LinkedIn".
    cleaned = re.sub(r"\s*[|\-–—]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", cleaned) if p.strip()]
    name = parts[0] if parts else cleaned
    role = parts[1] if len(parts) > 1 else None
    company = parts[2] if len(parts) > 2 else None

    if not name:
        return None
    return PersonCandidate(
        name=name,
        title=role,
        company=company,
        location=_extract_location(snippet),
        source_url=link or None,
        snippet=snippet or cleaned,
        raw=result,
    )


def parse_results(payload: dict) -> list[PersonCandidate]:
    """PURE: map a Brave web-search JSON response into PersonCandidate list (order kept)."""
    results = ((payload.get("web") or {}).get("results")) or []
    out: list[PersonCandidate] = []
    for result in results:
        cand = _parse_one(result)
        if cand is not None:
            out.append(cand)
    return out


class BraveClient:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def search(self, query: str, *, num: int = 10) -> dict:
        """Run one Brave web search. Raises TransientProviderError on 429/5xx (retryable)
        and ProviderError on 401/403 (bad key) — mirroring the app's other outbound-HTTP
        error mapping."""
        try:
            resp = self._client.get(
                _ENDPOINT,
                params={"q": query, "count": min(num, 20)},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._key,
                },
            )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"Brave request failed: {exc}") from exc

        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientProviderError(f"Brave transient error (HTTP {resp.status_code})")
        if resp.status_code in (401, 403):
            raise ProviderError("Brave rejected the API key (check BRAVE_API_KEY).")
        if resp.status_code != 200:
            raise ProviderError(f"Brave error (HTTP {resp.status_code}).")
        return resp.json()
