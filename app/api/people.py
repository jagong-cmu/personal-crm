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
