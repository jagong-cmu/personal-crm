"""Hunter.io email-finder client.

Given a name + company, returns the email Hunter has on file (or None). The email is
copied VERBATIM from Hunter's response — this module never guesses or synthesizes an
address. ``parse_response`` is PURE (fixture-testable); only ``find_email`` hits the net.
"""
from __future__ import annotations

import re

import httpx

from app.services.providers.base import (
    EmailResult,
    ProviderError,
    TransientProviderError,
)

_ENDPOINT = "https://api.hunter.io/v2/email-finder"
_TIMEOUT = 20.0


def _domain_from_company(company: str | None) -> str | None:
    """Best-effort domain guess Hunter can refine (it also accepts a raw company name)."""
    if not company:
        return None
    slug = re.sub(r"[^a-z0-9]", "", company.lower())
    return f"{slug}.com" if slug else None


def parse_response(payload: dict) -> EmailResult:
    """PURE: extract the email + confidence from a Hunter email-finder response."""
    data = payload.get("data") or {}
    email = data.get("email")
    confidence = data.get("score")
    if isinstance(email, str):
        email = email.strip() or None
    if not isinstance(confidence, int):
        confidence = None
    return EmailResult(email=email, confidence=confidence, raw=payload)


class HunterClient:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def find_email(self, name: str, company: str | None) -> EmailResult:
        """Look up ``name`` at ``company``. Returns EmailResult(None, ...) when Hunter has
        nothing (or when there's no company to search) — that is not an error."""
        params: dict[str, str] = {"api_key": self._key, "full_name": name}
        domain = _domain_from_company(company)
        if domain:
            params["domain"] = domain
        elif company:
            params["company"] = company
        else:
            # No company anchor → Hunter can't find a work email; skip the call.
            return EmailResult(email=None, confidence=None, raw={"skipped": "no company"})

        try:
            resp = self._client.get(_ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"Hunter request failed: {exc}") from exc

        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientProviderError(f"Hunter transient error (HTTP {resp.status_code})")
        if resp.status_code in (401, 403):
            raise ProviderError("Hunter rejected the API key (check HUNTER_API_KEY).")
        if resp.status_code == 404:
            # No result for this name/domain — a normal "no email found" outcome.
            return EmailResult(email=None, confidence=None, raw={"http": 404})
        if resp.status_code != 200:
            raise ProviderError(f"Hunter error (HTTP {resp.status_code}).")
        return parse_response(resp.json())
