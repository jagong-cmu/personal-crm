"""Labeled eval dataset + a deterministic offline embedder (T10).

The RAG eval harness asserts three things against a seeded fixture network:
  * intent accuracy   — classify_intent routes each question correctly (PURE, no DB/API)
  * recall@k          — retrieval surfaces the expected people for each question
  * citation grounding— every returned citation maps to a real seeded person (no fabrication)

To stay fully offline (no Voyage / no Anthropic), retrieval uses ``fake_embed`` — a
deterministic hashed bag-of-words vector. Texts sharing words get high cosine similarity,
so semantic recall is meaningful AND reproducible. The SAME function seeds the document
vectors and embeds the query, so the geometry is consistent.
"""
from __future__ import annotations

import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    """Tiny suffix stemmer so designer/designers and learn/learning share a bucket.

    Deliberately lighter than a real stemmer: just enough that morphological variants
    overlap WITHOUT the false collisions a crude prefix hash would create (e.g.
    'professional' vs 'professor'), which would leak signal into zero-result queries.
    """
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


# Interned token -> dimension map. Each distinct stemmed token gets its own slot, so two
# DIFFERENT words never collide into one bucket (a plain hash%dim would, producing false
# overlaps that leak signal into the zero-result cases). The fixture vocabulary is tiny
# (<< dim), so this is collision-free; it only wraps if vocab exceeds dim. Deterministic
# within a process (seeding interns the doc words first, queries reuse them).
_VOCAB: dict[str, int] = {}


def _index(token: str, dim: int) -> int:
    if token not in _VOCAB:
        _VOCAB[token] = len(_VOCAB) % dim
    return _VOCAB[token]


def fake_embed(text: str, dim: int) -> list[float]:
    """Deterministic stemmed bag-of-words embedding, L2-normalized. Offline Voyage stand-in.

    Texts sharing (stemmed) words get high cosine; texts sharing none are orthogonal
    (cosine 0), so genuinely-unrelated queries fall below the retrieval threshold —
    which is exactly what the zero-result eval cases assert.
    """
    vec = [0.0] * dim
    for raw in _TOKEN.findall(text.lower()):
        vec[_index(_stem(raw), dim)] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# --------------------------------------------------------------------------------------
# Fixture network: the "people" the eval queries run against.
# --------------------------------------------------------------------------------------

NETWORK = [
    # key            name                email                      company       title                         text (embedded)
    ("sarah_stripe", "Sarah Chen", "sarah@stripe.com", "Stripe", "Staff Engineer", "Sarah Chen — Staff Engineer at Stripe, payments infrastructure"),
    ("raj_stripe", "Raj Patel", "raj@stripe.com", "Stripe", "Product Manager", "Raj Patel — Product Manager at Stripe, billing"),
    ("mei_google", "Mei Lin", "mei@google.com", "Google", "Research Scientist", "Mei Lin — Research Scientist at Google, machine learning and NLP"),
    ("tom_google", "Tom Becker", "tom@google.com", "Google", "Engineering Manager", "Tom Becker — Engineering Manager at Google, infrastructure"),
    ("ana_openai", "Ana Duarte", "ana@openai.com", "OpenAI", "Researcher", "Ana Duarte — Researcher at OpenAI, reinforcement learning"),
    ("liu_acme", "Wei Liu", "wei@acme.io", "Acme", "Designer", "Wei Liu — Product Designer at Acme, design systems"),
    ("john_acme", "John Smith", "john@acme.io", "Acme", "Founder", "John Smith — Founder and CEO at Acme robotics startup"),
    ("priya_cmu", "Priya Nair", "priya@cmu.edu", "Carnegie Mellon", "Professor", "Priya Nair — Professor at Carnegie Mellon, computer security and cryptography"),
]

# Interactions for recency queries: (person_key, source_type, interaction_type, days_ago, subject)
INTERACTIONS = [
    ("sarah_stripe", "gmail", "email_received", 3, "Re: integration review"),
    ("sarah_stripe", "calendar", "meeting", 30, "Quarterly sync"),
    ("mei_google", "calendar", "meeting", 10, "ML collaboration"),
    ("priya_cmu", "gmail", "email_sent", 100, "Thesis feedback"),
]


# --------------------------------------------------------------------------------------
# Labeled cases.
#   intent:   expected classify_intent route
#   slots:    expected extracted slots (subset-checked)
#   relevant: person keys that SHOULD appear in top-k retrieval (None = intent-only case)
# --------------------------------------------------------------------------------------

CASES = [
    # --- structured: company ---
    {"q": "Who do I know at Stripe?", "intent": "company", "slots": {"company": "Stripe"}, "relevant": {"sarah_stripe", "raj_stripe"}},
    {"q": "anyone at Google?", "intent": "company", "slots": {"company": "Google"}, "relevant": {"mei_google", "tom_google"}},
    {"q": "people at Acme", "intent": "company", "slots": {"company": "Acme"}, "relevant": {"liu_acme", "john_acme"}},
    {"q": "which contacts work at OpenAI", "intent": "company", "slots": {"company": "OpenAI"}, "relevant": {"ana_openai"}},
    {"q": "colleagues at Carnegie Mellon", "intent": "company", "slots": {"company": "Carnegie Mellon"}, "relevant": {"priya_cmu"}},
    # --- structured: recency ---
    {"q": "When did I last talk to Sarah?", "intent": "recency", "slots": {"name": "Sarah"}, "relevant": {"sarah_stripe"}},
    {"q": "when did I last meet with Mei Lin", "intent": "recency", "slots": {"name": "Mei Lin"}, "relevant": {"mei_google"}},
    {"q": "when did I last email Priya", "intent": "recency", "slots": {"name": "Priya"}, "relevant": {"priya_cmu"}},
    {"q": "who did I most recently meet", "intent": "recency", "slots": {}, "relevant": None},
    # --- semantic ---
    {"q": "who works on machine learning", "intent": "semantic", "slots": {}, "relevant": {"mei_google", "ana_openai"}},
    {"q": "do I know any designers", "intent": "semantic", "slots": {}, "relevant": {"liu_acme"}},
    {"q": "find someone in computer security", "intent": "semantic", "slots": {}, "relevant": {"priya_cmu"}},
    {"q": "who is a founder of a startup", "intent": "semantic", "slots": {}, "relevant": {"john_acme"}},
    {"q": "people in payments infrastructure", "intent": "semantic", "slots": {}, "relevant": {"sarah_stripe"}},
    {"q": "tell me about my network", "intent": "semantic", "slots": {}, "relevant": None},
    # --- zero-result (semantic; nobody matches) ---
    {"q": "who do I know that plays professional basketball", "intent": "semantic", "slots": {}, "relevant": set()},
    {"q": "any astronauts in my contacts", "intent": "semantic", "slots": {}, "relevant": set()},
    # --- ambiguous (company + recency cues) -> classifier defers to Claude/hybrid ---
    {"q": "when did I last talk to anyone at Stripe", "intent": "ambiguous", "slots": {}, "relevant": None},
]
