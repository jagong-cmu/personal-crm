"""People API: provenance lookup (T16) + manual merge / un-merge (T14)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db.session import get_tenant_db
from app.models import Interaction, Person
from app.services import merge as merge_service
from app.services.entity_resolution import name_company_similarity, normalize_email

router = APIRouter(prefix="/people", tags=["people"])


def _tenant():
    return get_settings().tenant_uuid


@router.get("")
def list_people(limit: int = 50, offset: int = 0, db: Session = Depends(get_tenant_db)) -> dict:
    """List canonical (non-merged-away) people."""
    rows = db.scalars(
        select(Person)
        .where(Person.tenant_id == _tenant(), Person.merged_into_id.is_(None))
        .order_by(Person.display_name)
        .limit(min(limit, 200))
        .offset(offset)
    ).all()
    return {
        "people": [
            {
                "id": str(p.id),
                "display_name": p.display_name,
                "primary_email": p.primary_email,
                "company": p.company,
                "title": p.title,
            }
            for p in rows
        ]
    }


@router.get("/candidates")
def merge_candidates(db: Session = Depends(get_tenant_db)) -> dict:
    """Potential duplicate people for the manual-review queue.

    Surfaces pairs resolution left UNMERGED that still look like one person:
      * same normalized primary email on two people  -> strong (shared email)
      * company-gated name+company fuzzy in the review band (0.55..0.85)
    Ingest auto-merges at >=0.85, so survivors sit below that. The UI's Merge
    button posts {winner_id, loser_id} to POST /people/merge (winner = more
    provenance). Read-only: nothing is persisted until the user merges.
    """
    t = _tenant()
    people = db.scalars(
        select(Person)
        .where(Person.tenant_id == t, Person.merged_into_id.is_(None))
        .options(selectinload(Person.sources))
    ).all()

    def pack(p: Person) -> dict:
        return {
            "id": str(p.id),
            "display_name": p.display_name,
            "company": p.company,
            "primary_email": p.primary_email,
            "sources": sorted({s.source_type for s in p.sources}),
        }

    seen: set[tuple[str, str]] = set()
    cands: list[tuple[float, str, Person, Person]] = []

    by_email: dict[str, list[Person]] = {}
    for p in people:
        e = normalize_email(p.primary_email)
        if e:
            by_email.setdefault(e, []).append(p)
    for grp in by_email.values():
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                key = tuple(sorted((str(grp[i].id), str(grp[j].id))))
                if key not in seen:
                    seen.add(key)
                    cands.append((1.0, "shared email", grp[i], grp[j]))

    withco = [p for p in people if p.company and p.display_name]
    for i in range(len(withco)):
        for j in range(i + 1, len(withco)):
            a, b = withco[i], withco[j]
            key = tuple(sorted((str(a.id), str(b.id))))
            if key in seen:
                continue
            score = name_company_similarity(a.display_name, a.company, b.display_name, b.company)
            if 0.55 <= score < 0.999:
                seen.add(key)
                cands.append((score, "fuzzy name + company", a, b))

    cands.sort(key=lambda c: c[0], reverse=True)
    out = []
    for score, reason, a, b in cands[:25]:
        winner, loser = (a, b) if len(a.sources) >= len(b.sources) else (b, a)
        out.append(
            {
                "score": round(score, 2),
                "reason": reason,
                "winner_id": str(winner.id),
                "loser_id": str(loser.id),
                "a": pack(a),
                "b": pack(b),
            }
        )
    return {"candidates": out}


def _linkedin_url(person: Person) -> str | None:
    """Derive a real LinkedIn profile URL from provenance, if one was captured.

    LinkedIn CSV imports store the profile URL as the source_record_id (when present),
    and promoted Discover prospects (contactgen) carry it in raw_data['source_url'].
    Returns None when no stored URL exists — the UI then offers a name-based search.
    """
    for s in person.sources:
        if s.source_type == "linkedin" and (s.source_record_id or "").startswith("http"):
            return s.source_record_id
        if s.source_type == "contactgen":
            url = (s.raw_data or {}).get("source_url")
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


@router.get("/{person_id}")
def get_person(person_id: uuid.UUID, db: Session = Depends(get_tenant_db)) -> dict:
    """Person + provenance. Uses selectinload to fetch sources in one extra query (no N+1, T16)."""
    person = db.scalar(
        select(Person)
        .where(Person.tenant_id == _tenant(), Person.id == person_id)
        .options(selectinload(Person.sources))
    )
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")

    interactions = db.scalars(
        select(Interaction)
        .where(
            Interaction.tenant_id == _tenant(),
            Interaction.person_id == person_id,
            Interaction.deleted_at.is_(None),
        )
        .order_by(Interaction.occurred_at.desc().nullslast())
        .limit(20)
    ).all()

    return {
        "id": str(person.id),
        "display_name": person.display_name,
        "primary_email": person.primary_email,
        "company": person.company,
        "title": person.title,
        "linkedin_url": _linkedin_url(person),
        "merged_into_id": str(person.merged_into_id) if person.merged_into_id else None,
        "sources": [
            {
                "source_type": s.source_type,
                "source_record_id": s.source_record_id,
                "matched_confidence": s.matched_confidence,
            }
            for s in person.sources
        ],
        "recent_interactions": [
            {
                "source_type": i.source_type,
                "interaction_type": i.interaction_type,
                "occurred_at": i.occurred_at.isoformat() if i.occurred_at else None,
                "subject": i.subject,
            }
            for i in interactions
        ],
    }


class MergeRequest(BaseModel):
    winner_id: uuid.UUID
    loser_id: uuid.UUID


class UnmergeRequest(BaseModel):
    loser_id: uuid.UUID


@router.post("/merge")
def merge_people(req: MergeRequest, db: Session = Depends(get_tenant_db)) -> dict:
    try:
        return merge_service.merge(db, _tenant(), req.winner_id, req.loser_id)
    except merge_service.MergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/unmerge")
def unmerge_people(req: UnmergeRequest, db: Session = Depends(get_tenant_db)) -> dict:
    try:
        return merge_service.unmerge(db, _tenant(), req.loser_id)
    except merge_service.MergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
