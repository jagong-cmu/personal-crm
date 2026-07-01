"""Response-likelihood scoring for Discover prospects — PURE and unit-testable.

Given how closely a prospect overlaps with the user's background, produce a 0..100
"likely to respond" score plus a transparent per-feature breakdown. No network, no DB:
the one signal that needs embeddings (interest similarity) is computed by the caller and
passed in as a float, so this whole module is deterministic and testable offline.

WEIGHTING (per product requirement): geography and school matter MOST, then company /
industry, then general interests LEAST — a shared hometown or alma mater is a stronger
reason to reply than an overlapping topic.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

#: Feature weights, highest priority first. Must sum to 1.0 (asserted below) so the
#: weighted sum lands in [0, 1] before scaling to 0..100.
WEIGHTS: dict[str, float] = {
    "geography": 0.30,
    "school": 0.30,
    "company": 0.25,
    "interest": 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "scoring weights must sum to 1.0"

#: Tokens that carry no discriminating signal for school/company matching.
_STOP = {
    "the", "of", "and", "at", "inc", "llc", "ltd", "co", "corp", "company",
    "university", "college", "school", "institute", "technology",
}


def _tokens(s: str | None) -> set[str]:
    """Lowercase alphanumeric token set with stopwords removed."""
    if not s:
        return set()
    raw = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in raw if t not in _STOP and len(t) > 1}


def geography_match(user_loc: str | None, prospect_loc: str | None) -> float:
    """1.0 exact location, 0.6 shared city/region token, 0.3 weak overlap, else 0.0."""
    a, b = _tokens(user_loc), _tokens(prospect_loc)
    if not a or not b:
        return 0.0
    if (user_loc or "").strip().lower() == (prospect_loc or "").strip().lower():
        return 1.0
    shared = a & b
    if not shared:
        return 0.0
    # Proportion of the smaller location's tokens that overlap → graded confidence.
    ratio = len(shared) / min(len(a), len(b))
    if ratio >= 0.5:
        return 0.6
    return 0.3


def _list_match(user_items: list[str] | None, prospect_item: str | None) -> float:
    """1.0 if any user item matches the prospect's (full normalized-token equality),
    0.5 on partial token overlap, else 0.0. Shared by school + company matching."""
    if not user_items or not prospect_item:
        return 0.0
    p = _tokens(prospect_item)
    if not p:
        return 0.0
    best = 0.0
    for item in user_items:
        u = _tokens(item)
        if not u:
            continue
        if u == p:
            return 1.0
        if not (u & p):
            continue
        # A subset only counts as a full match when the shared side has real substance
        # (≥2 tokens) — otherwise "Carnegie Institute" would falsely equal "Carnegie
        # Mellon" off a single shared token. Weak/single-token overlap grades to 0.5.
        if (u <= p or p <= u) and min(len(u), len(p)) >= 2:
            return 1.0
        best = max(best, 0.5)
    return best


def school_match(user_schools: list[str] | None, prospect_school: str | None) -> float:
    return _list_match(user_schools, prospect_school)


def company_match(user_companies: list[str] | None, prospect_company: str | None) -> float:
    return _list_match(user_companies, prospect_company)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity clamped to [0, 1]. Empty/zero vectors → 0.0."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


@dataclass
class ScoreResult:
    score: int  # 0..100
    features: dict[str, float]  # each 0..1
    weights: dict[str, float]
    contributions: dict[str, float]  # weight * feature, each 0..1

    def as_breakdown(self) -> dict:
        """JSON-serializable shape for the prospect.score_breakdown column / UI."""
        return {
            "score": self.score,
            "features": self.features,
            "weights": self.weights,
            "contributions": self.contributions,
        }


def score_prospect(*, geography: float, school: float, company: float, interest: float) -> ScoreResult:
    """Combine the four [0,1] feature signals into a weighted 0..100 score.

    Because geography and school carry the largest weights, raising either moves the
    score more than raising ``interest`` by the same amount — the transparent encoding
    of "geography & school first, general interests last".
    """
    features = {
        "geography": round(float(geography), 4),
        "school": round(float(school), 4),
        "company": round(float(company), 4),
        "interest": round(float(interest), 4),
    }
    contributions = {k: round(WEIGHTS[k] * features[k], 4) for k in WEIGHTS}
    total = round(100 * sum(contributions.values()))
    return ScoreResult(
        score=int(total),
        features=features,
        weights=dict(WEIGHTS),
        contributions=contributions,
    )
