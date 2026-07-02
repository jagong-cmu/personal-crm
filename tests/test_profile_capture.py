"""Pure unit tests for Discover profile capture (parsing only — no network)."""
from __future__ import annotations

from app.services import profile_capture
from app.services.profile_capture import (
    parse_manual_form,
    parse_scraped_html,
    scrape_linkedin,
)


def test_parse_manual_form_splits_lists():
    data = parse_manual_form(
        {
            "display_name": "  Sam Mathew ",
            "headline": "ML Engineer",
            "location": "Pittsburgh, PA",
            "schools": "Carnegie Mellon University, MIT",
            "companies": ["Stripe", "  "],
            "skills": "rag\nembeddings; python",
            "about": "",
        }
    )
    assert data.display_name == "Sam Mathew"
    assert data.schools == ["Carnegie Mellon University", "MIT"]
    assert data.companies == ["Stripe"]
    assert data.skills == ["rag", "embeddings", "python"]
    assert data.about is None  # empty string normalizes to None
    assert data.raw["source"] == "manual"


def test_parse_scraped_html_from_og_tags():
    html = (
        '<html><head>'
        '<meta property="og:title" content="Sam Mathew - ML Engineer at Stripe | LinkedIn"/>'
        '<meta property="og:description" content="Building RAG systems. CMU alum."/>'
        '</head><body>hi</body></html>'
    )
    data = parse_scraped_html(html)
    assert data is not None
    assert data.display_name == "Sam Mathew"
    assert data.headline == "ML Engineer at Stripe"
    assert "RAG" in (data.about or "")


def test_parse_scraped_html_authwall_returns_none():
    html = '<html><body><div class="authwall">Sign in to LinkedIn to continue</div></body></html>'
    assert parse_scraped_html(html) is None
    assert parse_scraped_html("") is None


def test_scrape_linkedin_rejects_non_linkedin_url():
    status, data = scrape_linkedin("https://example.com/in/someone")
    assert status == "fallback"
    assert data is None


def test_scrape_linkedin_falls_back_on_http_error(monkeypatch):
    def boom(*a, **k):
        import httpx
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(profile_capture.httpx, "get", boom)
    status, data = scrape_linkedin("https://www.linkedin.com/in/someone")
    assert status == "fallback"
    assert data is None


def test_scrape_linkedin_parses_success(monkeypatch):
    class _Resp:
        status_code = 200
        text = (
            '<meta property="og:title" content="Jane Doe - Founder | LinkedIn"/>'
            '<meta property="og:description" content="Climate tech."/>'
        )

    monkeypatch.setattr(profile_capture.httpx, "get", lambda *a, **k: _Resp())
    status, data = scrape_linkedin("https://www.linkedin.com/in/janedoe")
    assert status == "scraped"
    assert data.display_name == "Jane Doe"
    assert data.headline == "Founder"
