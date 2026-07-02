"""Pure unit tests for Discover providers (query building + response parsing).

Only the parse/build helpers are exercised — no live HTTP. The HTTP client methods are
tested against a fake httpx client so no network or keys are needed.
"""
from __future__ import annotations

from app.services.profile_capture import UserProfileData
from app.services.providers import hunter
from app.services.providers.base import ProviderError, TransientProviderError
from app.services.providers.brave import BraveClient, build_queries, parse_results
from app.services.providers.hunter import HunterClient, parse_response


def _profile() -> UserProfileData:
    return UserProfileData(
        display_name="Sam Mathew",
        headline="ML Engineer",
        location="Pittsburgh, PA",
        schools=["Carnegie Mellon University"],
        companies=["Stripe"],
        skills=["rag", "embeddings"],
    )


def test_build_queries_uses_signals_and_is_capped():
    qs = build_queries(_profile())
    assert qs, "expected at least one query"
    assert len(qs) <= 4
    assert all("site:linkedin.com/in" in q for q in qs)
    # The strongest signal (school) should appear in at least one query.
    assert any("Carnegie Mellon University" in q for q in qs)


def test_build_queries_empty_profile():
    assert build_queries(UserProfileData()) == []


def test_brave_parse_results():
    payload = {
        "web": {
            "results": [
                {
                    "title": "Jane Doe - Staff Engineer - Stripe | LinkedIn",
                    "url": "https://www.linkedin.com/in/janedoe",
                    "description": "Staff Engineer at Stripe · San Francisco Bay Area",
                },
                {"title": "", "url": "x"},  # dropped: no title
            ]
        }
    }
    out = parse_results(payload)
    assert len(out) == 1
    c = out[0]
    assert c.name == "Jane Doe"
    assert c.title == "Staff Engineer"
    assert c.company == "Stripe"
    assert c.source_url == "https://www.linkedin.com/in/janedoe"
    assert "San Francisco" in (c.location or "")


def test_brave_ignores_prose_as_location():
    # Brave descriptions are prose; a real place has "City, ST"/"City, Country"/"... Area".
    payload = {
        "web": {
            "results": [
                {
                    "title": "Emil Krabbe - Researcher | LinkedIn",
                    "url": "https://www.linkedin.com/in/emilkrabbe",
                    "description": "Research at Carnegie Mellon University as a research scholar",
                }
            ]
        }
    }
    out = parse_results(payload)
    assert len(out) == 1
    assert out[0].location is None  # no place-shaped tail → no garbage location


def test_hunter_parse_response():
    present = parse_response({"data": {"email": "jane@stripe.com", "score": 95}})
    assert present.email == "jane@stripe.com"
    assert present.confidence == 95
    absent = parse_response({"data": {"email": None}})
    assert absent.email is None


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, params=None, headers=None):
        return self._resp


def test_brave_maps_bad_key_to_provider_error():
    client = BraveClient("k", client=_FakeClient(_FakeResp(403)))
    try:
        client.search("q")
        assert False, "expected ProviderError"
    except ProviderError:
        pass


def test_brave_maps_5xx_to_transient():
    client = BraveClient("k", client=_FakeClient(_FakeResp(503)))
    try:
        client.search("q")
        assert False, "expected TransientProviderError"
    except TransientProviderError:
        pass


def test_hunter_no_company_skips_call():
    # No company anchor → returns empty result without any HTTP call.
    client = HunterClient("k", client=_FakeClient(_FakeResp(500)))
    res = client.find_email("Jane Doe", None)
    assert res.email is None


def test_hunter_404_is_no_result_not_error():
    client = HunterClient("k", client=_FakeClient(_FakeResp(404)))
    res = client.find_email("Jane Doe", "Stripe")
    assert res.email is None


def test_domain_guess():
    assert hunter._domain_from_company("Stripe Inc") == "stripeinc.com"
    assert hunter._domain_from_company(None) is None
