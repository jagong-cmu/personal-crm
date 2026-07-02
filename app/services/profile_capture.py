"""Capture the user's OWN LinkedIn background for Discover.

Two paths, both landing in a ``UserProfileData``:
  * ``parse_manual_form`` — the reliable path. The manual import form is the primary
    source because LinkedIn auth-walls scrapers.
  * ``scrape_linkedin`` — best-effort fetch of a pasted profile URL. LinkedIn almost
    always returns an auth-wall / HTTP 999 to unauthenticated clients, so this returns
    ``("fallback", None)`` in the common case and NEVER raises — the caller then shows
    the manual form.

Parsing (``parse_manual_form`` / ``parse_scraped_html``) is PURE so it is unit-testable
against saved fixtures without any network.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
_TIMEOUT = 10.0


@dataclass
class UserProfileData:
    display_name: str | None = None
    headline: str | None = None
    location: str | None = None
    schools: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    about: str | None = None
    raw: dict = field(default_factory=dict)


def _split_list(value) -> list[str]:
    """Accept a list or a comma/newline-separated string → clean list of non-empty items."""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[,\n;]+", str(value))
    return [s.strip() for s in items if s and s.strip()]


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_manual_form(form: dict) -> UserProfileData:
    """PURE: normalize a manual-import form dict into UserProfileData.

    Accepts list fields (schools/companies/skills) either as JSON arrays or as
    comma/newline-separated strings, so both the API model and a raw textarea work.
    """
    return UserProfileData(
        display_name=_clean(form.get("display_name")),
        headline=_clean(form.get("headline")),
        location=_clean(form.get("location")),
        schools=_split_list(form.get("schools")),
        companies=_split_list(form.get("companies")),
        skills=_split_list(form.get("skills")),
        about=_clean(form.get("about")),
        raw={"source": "manual", "form": form},
    )


def _meta(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    return _clean(m.group(1)) if m else None


def _looks_auth_walled(html: str) -> bool:
    low = html.lower()
    markers = ("authwall", "sign in to linkedin", "join linkedin", "please enable javascript")
    return any(m in low for m in markers)


def parse_scraped_html(html: str) -> UserProfileData | None:
    """PURE best-effort parse of a public LinkedIn profile page.

    Pulls what public meta tags expose (og:title / og:description). Returns None when the
    page is an auth-wall or carries no usable signal, so the caller falls back to the
    manual form. Deliberately conservative — better to fall back than import garbage.
    """
    if not html or _looks_auth_walled(html):
        return None

    title = _meta(html, "og:title") or _meta(html, "twitter:title")
    desc = _meta(html, "og:description") or _meta(html, "twitter:description")
    if not title and not desc:
        return None

    name = None
    headline = None
    if title:
        # og:title is usually "Name - Headline | LinkedIn"
        cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title).strip()
        parts = cleaned.split(" - ", 1)
        name = _clean(parts[0])
        if len(parts) > 1:
            headline = _clean(parts[1])

    return UserProfileData(
        display_name=name,
        headline=headline,
        about=desc,
        raw={"source": "scrape", "og_title": title, "og_description": desc},
    )


def scrape_linkedin(url: str) -> tuple[str, UserProfileData | None]:
    """Best-effort fetch + parse of a LinkedIn profile URL. NEVER raises.

    Returns ("scraped", data) on success, else ("fallback", None) — the overwhelmingly
    common outcome, since LinkedIn blocks unauthenticated profile fetches.
    """
    url = (url or "").strip()
    if not re.match(r"^https?://([a-z0-9-]+\.)?linkedin\.com/", url, re.IGNORECASE):
        return "fallback", None
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.info("linkedin scrape failed (%s); falling back to manual form", type(exc).__name__)
        return "fallback", None

    if resp.status_code != 200:
        logger.info("linkedin scrape got HTTP %s; falling back to manual form", resp.status_code)
        return "fallback", None

    data = parse_scraped_html(resp.text)
    if data is None:
        return "fallback", None
    return "scraped", data
