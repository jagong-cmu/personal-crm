"""RAG query engine — Slice 0: semantic retrieval + cited synthesis.

Slice 0 is pure vector search to prove the loop end-to-end. The heuristic-first
intent classifier (decision P1) and the structured/hybrid SQL path (brief step 8)
layer on in a later slice — this returns the same shape they will:
{answer, citations[]}.
"""
from __future__ import annotations

from dataclasses import dataclass

import anthropic
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmbeddingChunk, Person
from app.services.embedding import embed_query

_SYSTEM = (
    "You answer questions about the user's professional network using ONLY the "
    "provided context. Cite the source of each fact inline, e.g. '(via Contacts)'. "
    "If the context does not contain the answer, say so plainly — do not guess. "
    "Respond with the answer only: no preamble, no reasoning, no meta-commentary."
)

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


def query(db: Session, tenant_id, question: str) -> QueryResult:
    qvec = embed_query(question)
    distance = EmbeddingChunk.embedding.cosine_distance(qvec)

    rows = (
        db.query(EmbeddingChunk, Person, distance.label("distance"))
        .join(Person, Person.id == EmbeddingChunk.person_id)
        .filter(EmbeddingChunk.tenant_id == tenant_id)
        .order_by(distance)
        .limit(get_settings().rag_top_k)
        .all()
    )

    citations: list[Citation] = []
    context_lines: list[str] = []
    for i, (chunk, person, dist) in enumerate(rows, start=1):
        score = 1.0 - float(dist)
        citations.append(
            Citation(
                person=person.display_name,
                company=person.company,
                email=person.primary_email,
                source_type=chunk.source_type,
                score=round(score, 4),
                snippet=chunk.chunk_text,
            )
        )
        context_lines.append(
            f"[{i}] {person.display_name or 'Unknown'}"
            f"{f' at {person.company}' if person.company else ''} "
            f"(via {chunk.source_type.title()}): {chunk.chunk_text}"
        )

    if not citations:
        return QueryResult(
            answer="I don't have anyone in your network indexed yet. "
            "Connect a source and run a sync first.",
            citations=[],
        )

    context = "\n".join(context_lines)
    # Synthesis runs without extended thinking to keep the interactive query snappy;
    # the "answer only" system instruction prevents Opus 4.8 from leaking reasoning
    # into the visible response. For harder analytic questions, add
    # thinking={"type": "adaptive"} here.
    msg = _anthropic().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=2048,
        system=_SYSTEM,
        messages=[
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
        ],
    )
    if msg.stop_reason == "refusal":
        return QueryResult(
            answer="I wasn't able to answer that. Try rephrasing the question.",
            citations=citations,
        )
    answer = "".join(block.text for block in msg.content if block.type == "text")
    return QueryResult(answer=answer, citations=citations)
