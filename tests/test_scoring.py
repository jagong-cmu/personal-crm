"""Pure unit tests for the Discover response-likelihood scorer (no DB, no network)."""
from __future__ import annotations

from app.services import scoring
from app.services.scoring import (
    company_match,
    cosine,
    geography_match,
    school_match,
    score_prospect,
)


def test_weights_sum_to_one_and_order():
    w = scoring.WEIGHTS
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # Geography and school are the highest-priority signals; interests the lowest.
    assert w["geography"] == w["school"]
    assert w["geography"] > w["company"] > w["interest"]


def test_geography_match_grades():
    assert geography_match("San Francisco", "San Francisco") == 1.0
    assert geography_match("San Francisco, CA", "San Francisco Bay Area") == 0.6
    assert geography_match("Boston, MA", "Austin, TX") == 0.0
    assert geography_match(None, "Boston") == 0.0


def test_school_and_company_match():
    assert school_match(["Carnegie Mellon University"], "Carnegie Mellon University") == 1.0
    # Partial token overlap (shared distinctive token) → 0.5.
    assert school_match(["Carnegie Mellon University"], "Carnegie Institute") == 0.5
    assert school_match(["MIT"], "Stanford") == 0.0
    assert company_match(["Stripe"], "Stripe") == 1.0
    assert company_match([], "Stripe") == 0.0


def test_cosine_edges():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_score_is_deterministic_and_bounded():
    r = score_prospect(geography=1.0, school=1.0, company=0.0, interest=0.0)
    # geography(0.30) + school(0.30) = 0.60 → 60
    assert r.score == 60
    assert r.features == {"geography": 1.0, "school": 1.0, "company": 0.0, "interest": 0.0}
    assert r.contributions["geography"] == 0.30
    full = score_prospect(geography=1.0, school=1.0, company=1.0, interest=1.0)
    assert full.score == 100
    zero = score_prospect(geography=0.0, school=0.0, company=0.0, interest=0.0)
    assert zero.score == 0


def test_geography_and_school_outweigh_interest():
    """The core product requirement, encoded as a property: bumping geography or school
    raises the score more than bumping interest by the same delta."""
    base = score_prospect(geography=0.0, school=0.0, company=0.0, interest=0.0).score
    geo = score_prospect(geography=0.5, school=0.0, company=0.0, interest=0.0).score
    sch = score_prospect(geography=0.0, school=0.5, company=0.0, interest=0.0).score
    inter = score_prospect(geography=0.0, school=0.0, company=0.0, interest=0.5).score
    assert geo - base > inter - base
    assert sch - base > inter - base


def test_breakdown_is_json_serializable_shape():
    r = score_prospect(geography=0.6, school=0.0, company=1.0, interest=0.4)
    b = r.as_breakdown()
    assert set(b) == {"score", "features", "weights", "contributions"}
    assert b["score"] == r.score
