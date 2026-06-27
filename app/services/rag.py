"""RAG query engine (T9): heuristic-first intent routing + cited synthesis.

PIPELINE
  1. classify_intent(question)  — PURE heuristic (regex/keyword). Returns one of
       company  -> "who do I know at <X>"        (structured: people.company match)
       recency  -> "when did I last talk to <X>"  (structured: interactions timeline)
       semantic -> everything else               (pgvector cosine search)
       ambiguous-> mixed signals
  2. On 'ambiguous', ask Claude to pick (company|recency|semantic). If that call fails
     for ANY reason we FALL BACK to hybrid retrieval — a query is never hard-failed by
     the classifier (decision P1).
  3. Retrieve (structured SQL / semantic vectors / hybrid union), then synthesize a
     grounded answer with inline citations. Return shape is stable: {answer, citations}.

Retrieval always filters merged-away people (merged_into_id IS NULL) and soft-deleted
interactions (deleted_at IS NULL).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmbeddingChunk, Interaction, Person
from app.services.embedding import embed_query

_SYSTEM = (
    "You answer questions about the user's professional network using ONLY the "
    "provided context. Cite the source of each fact inline, e.g. '(via Contacts)'. "
    "If the context does not contain the answer, say so plainly — do not guess. "
    "Respond with the answer only: no preamble, no reasoning, no meta-commentary."
)

#: Minimum cosine similarity for a semantic hit to count. Below this the match is noise,
#: so an unrelated question ("any astronauts?") returns nothing rather than the k nearest
#: strangers — the retrieval layer's defense against forced/irrelevant citations.
_SEMANTIC_MIN_SCORE = 0.05

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return _client


@dataclass
class Citation:
    person: str | None
    company: str | None
    email: str | None
    source_type: str
    score: float
    snippet: str


@dataclass
class QueryResult:
    answer: str
    citations: list[Citation]
    intent: str = "semantic"  # which route served the query (observability / eval)


# --------------------------------------------------------------------------------------
# Intent classification (PURE — unit-testable offline)
# --------------------------------------------------------------------------------------

_COMPANY_CUES = re.compile(
    r"\b(who|anyone|any one|people|person|contacts?|connections?|colleagues?)\b", re.I
)
_AT_COMPANY = re.compile(r"\b(?:work(?:s|ing)?\s+)?at\s+(.+)$", re.I)
_RECENCY = re.compile(
    r"\b(last|recently|most recent)\b.*\b(talk|spoke|speak|chat|met|meet|meeting|"
    r"email|emailed|contact|saw|see|connect)\w*\b",
    re.I,
)
_RECENCY_NAME = re.compile(
    r"\b(?:talk(?:ed)?\s+to|spoke\s+(?:to|with)|speak\s+(?:to|with)|met\s+with|met|"
    r"meet|email(?:ed)?|contact(?:ed)?|saw|see|connect(?:ed)?\s+with)\s+(.+)$",
    re.I,
)
_STOP_TAIL = re.compile(r"[\s?.!,]+$")


def _clean_slot(raw: str) -> str:
    s = _STOP_TAIL.sub("", raw.strip().strip("\"'"))
    # Cut trailing locational/temporal qualifiers ("...in SF", "...about pricing").
    s = re.split(r"\b(?:in|about|regarding|re|on|for)\b", s, maxsplit=1, flags=re.I)[0]
    # Drop a leading preposition the verb pattern may have swept in ("meet WITH Mei").
    s = re.sub(r"^(?:with|to)\s+", "", s.strip(), flags=re.I)
    return s.strip()


def classify_intent(question: str) -> tuple[str, dict]:
    """Return (intent, slots). intent in {company, recency, semantic, ambiguous}. Pure."""
    q = question.strip()
    company_match = _AT_COMPANY.search(q)
    has_company = bool(company_match and _COMPANY_CUES.search(q))
    recency_match = _RECENCY.search(q)
    has_recency = bool(recency_match)

    if has_company and has_recency:
        return "ambiguous", {}
    if has_company:
        return "company", {"company": _clean_slot(company_match.group(1))}
    if has_recency:
        name_m = _RECENCY_NAME.search(q)
        slots = {"name": _clean_slot(name_m.group(1))} if name_m else {}
        return "recency", slots
    return "semantic", {}


def _claude_classify(question: str) -> tuple[str, dict]:
    """Ask Claude to disambiguate. Raises on any failure (caller falls back to hybrid)."""
    msg = _anthropic().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=200,
        system=(
            "Classify the network query as JSON only: "
            '{"intent": "company"|"recency"|"semantic", "company": str|null, "name": str|null}. '
            "company = find people at an organization; recency = when last in contact with "
            "someone; semantic = anything else. No prose."
        ),
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    intent = data.get("intent", "semantic")
    slots = {k: v for k, v in (("company", data.get("company")), ("name", data.get("name"))) if v}
    return intent, slots


# --------------------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------------------


def _retrieve_company(db: Session, tenant_id, company: str) -> list[Citation]:
    rows = db.scalars(
        select(Person)
        .where(
            Person.tenant_id == tenant_id,
            Person.merged_into_id.is_(None),
            Person.company.ilike(f"%{company}%"),
        )
        .limit(get_settings().rag_top_k)
    ).all()
    return [
        Citation(
            person=p.display_name,
            company=p.company,
            email=p.primary_email,
            source_type="contacts",
            score=1.0,
            snippet=f"{p.display_name or 'Unknown'} — {p.title or 'role unknown'} at {p.company}",
        )
        for p in rows
    ]


def _retrieve_recency(db: Session, tenant_id, name: str | None) -> list[Citation]:
    stmt = (
        select(Interaction, Person)
        .join(Person, Person.id == Interaction.person_id)
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.deleted_at.is_(None),
            Person.merged_into_id.is_(None),
        )
    )
    if name:
        stmt = stmt.where(Person.display_name.ilike(f"%{name}%"))
    stmt = stmt.order_by(Interaction.occurred_at.desc().nullslast()).limit(
        get_settings().rag_top_k
    )
    out: list[Citation] = []
    for interaction, person in db.execute(stmt).all():
        when = (
            interaction.occurred_at.date().isoformat()
            if interaction.occurred_at
            else "unknown date"
        )
        out.append(
            Citation(
                person=person.display_name,
                company=person.company,
                email=person.primary_email,
                source_type=interaction.source_type,
                score=1.0,
                snippet=f"{interaction.interaction_type} on {when}: {interaction.subject or ''}".strip(),
            )
        )
    return out


def _retrieve_semantic(db: Session, tenant_id, question: str) -> list[Citation]:
    qvec = embed_query(question)
    distance = EmbeddingChunk.embedding.cosine_distance(qvec)
    rows = (
        db.query(EmbeddingChunk, Person, distance.label("distance"))
        .join(Person, Person.id == EmbeddingChunk.person_id)
        .filter(
            EmbeddingChunk.tenant_id == tenant_id,
            Person.merged_into_id.is_(None),
        )
        .order_by(distance)
        .limit(get_settings().rag_top_k)
        .all()
    )
    return [
        Citation(
            person=person.display_name,
            company=person.company,
            email=person.primary_email,
            source_type=chunk.source_type,
            score=round(1.0 - float(dist), 4),
            snippet=chunk.chunk_text,
        )
        for chunk, person, dist in rows
        if (1.0 - float(dist)) >= _SEMANTIC_MIN_SCORE
    ]


def _dedupe(citations: list[Citation]) -> list[Citation]:
    seen: set[tuple] = set()
    out: list[Citation] = []
    for c in citations:
        key = (c.person, c.company, c.snippet)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _retrieve(db: Session, tenant_id, intent: str, slots: dict, question: str) -> list[Citation]:
    if intent == "company" and slots.get("company"):
        return _retrieve_company(db, tenant_id, slots["company"])
    if intent == "recency":
        return _retrieve_recency(db, tenant_id, slots.get("name"))
    if intent == "semantic":
        return _retrieve_semantic(db, tenant_id, question)
    # hybrid: union structured (best-effort) + semantic.
    hybrid: list[Citation] = []
    if slots.get("company"):
        hybrid += _retrieve_company(db, tenant_id, slots["company"])
    hybrid += _retrieve_recency(db, tenant_id, slots.get("name"))
    hybrid += _retrieve_semantic(db, tenant_id, question)
    return _dedupe(hybrid)[: get_settings().rag_top_k]


# --------------------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------------------


def query(db: Session, tenant_id, question: str) -> QueryResult:
    intent, slots = classify_intent(question)
    if intent == "ambiguous":
        try:
            intent, slots = _claude_classify(question)
        except Exception:  # noqa: BLE001 — never hard-fail on classifier error (P1)
            intent = "hybrid"

    citations = _retrieve(db, tenant_id, intent, slots, question)

    if not citations:
        return QueryResult(
            answer="I don't have anything matching that in your network yet. "
            "Connect a source and run a sync first.",
            citations=[],
            intent=intent,
        )

    context_lines = [
        f"[{i}] {c.person or 'Unknown'}"
        f"{f' at {c.company}' if c.company else ''} "
        f"(via {c.source_type.title()}): {c.snippet}"
        for i, c in enumerate(citations, start=1)
    ]
    context = "\n".join(context_lines)
    msg = _anthropic().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=2048,
        system=_SYSTEM,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}],
    )
    if msg.stop_reason == "refusal":
        return QueryResult(
            answer="I wasn't able to answer that. Try rephrasing the question.",
            citations=citations,
            intent=intent,
        )
    answer = "".join(block.text for block in msg.content if block.type == "text")
    return QueryResult(answer=answer, citations=citations, intent=intent)
