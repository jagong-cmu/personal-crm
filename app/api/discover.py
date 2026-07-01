"""Discover API: user profile capture + contact generation (prospects).

Endpoints:
  GET   /discover/profile            — current profile + whether providers are configured
  PUT   /discover/profile            — save/replace the profile (manual import)
  POST  /discover/profile/scrape     — best-effort LinkedIn URL scrape (does NOT persist)
  POST  /discover/run                — generate prospects (needs Brave + Hunter keys)
  GET   /discover/prospects          — list prospects (optionally by status), score desc
  PATCH /discover/prospects/{id}     — update status (saved | dismissed)
  POST  /discover/prospects/{id}/save — promote a prospect into the real network
"""
from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_tenant_db
from app.models import Prospect
from app.services import discover, profile_capture
from app.services.providers import ProviderError, get_provider

router = APIRouter(prefix="/discover", tags=["discover"])


def _tenant():
    return get_settings().tenant_uuid


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------


class ProfileIn(BaseModel):
    display_name: str | None = None
    headline: str | None = None
    location: str | None = None
    schools: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    about: str | None = None


class ScrapeIn(BaseModel):
    url: str


class RunIn(BaseModel):
    max_candidates: int | None = None


class StatusIn(BaseModel):
    status: str  # saved | dismissed


# --------------------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------------------


def _profile_out(p) -> dict | None:
    if p is None:
        return None
    return {
        "display_name": p.display_name,
        "headline": p.headline,
        "location": p.location,
        "schools": list(p.schools or []),
        "companies": list(p.companies or []),
        "skills": list(p.skills or []),
        "about": p.about,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _prospect_out(p: Prospect) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "email": p.email,
        "phone": p.phone,
        "company": p.company,
        "title": p.title,
        "location": p.location,
        "school": p.school,
        "source_url": p.source_url,
        "score": p.score,
        "score_breakdown": p.score_breakdown,
        "relation_summary": p.relation_summary,
        "status": p.status,
        "contactless": not (p.email or p.phone),
        "promoted_person_id": str(p.promoted_person_id) if p.promoted_person_id else None,
    }


# --------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------


@router.get("/profile")
def get_profile(db: Session = Depends(get_tenant_db)) -> dict:
    profile = discover.get_profile(db, _tenant())
    return {
        "profile": _profile_out(profile),
        "discovery_enabled": get_settings().discovery_enabled,
    }


@router.put("/profile")
def save_profile(req: ProfileIn, db: Session = Depends(get_tenant_db)) -> dict:
    data = profile_capture.parse_manual_form(req.model_dump())
    profile = discover.save_profile(db, _tenant(), data)
    return {"profile": _profile_out(profile)}


@router.post("/profile/scrape")
def scrape_profile(req: ScrapeIn) -> dict:
    """Best-effort scrape. Never persists — returns a draft for the user to confirm/edit,
    or status='fallback' telling the UI to show the manual form."""
    status, data = profile_capture.scrape_linkedin(req.url)
    if status == "scraped" and data is not None:
        return {
            "status": "scraped",
            "draft": {k: v for k, v in asdict(data).items() if k != "raw"},
            "message": "Imported from LinkedIn — review and save.",
        }
    return {
        "status": "fallback",
        "draft": None,
        "message": "Couldn't read that profile (LinkedIn blocks automated access). "
        "Enter your details below.",
    }


# --------------------------------------------------------------------------------------
# Discovery run + prospects
# --------------------------------------------------------------------------------------


@router.post("/run")
def run(req: RunIn, db: Session = Depends(get_tenant_db)) -> dict:
    provider = get_provider(get_settings())
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="Discover is not configured. Add BRAVE_API_KEY and HUNTER_API_KEY to "
            ".env to enable contact generation.",
        )
    kwargs = {} if req.max_candidates is None else {"max_candidates": req.max_candidates}
    try:
        summary = discover.run_discovery(db, _tenant(), provider=provider, **kwargs)
    except ValueError as exc:  # no profile yet
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProviderError as exc:  # bad key / provider outage
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"summary": asdict(summary)}


@router.get("/prospects")
def list_prospects(status: str | None = None, db: Session = Depends(get_tenant_db)) -> dict:
    stmt = select(Prospect).where(Prospect.tenant_id == _tenant())
    if status:
        stmt = stmt.where(Prospect.status == status)
    stmt = stmt.order_by(Prospect.score.desc(), Prospect.created_at.desc())
    rows = db.scalars(stmt).all()
    return {"prospects": [_prospect_out(p) for p in rows]}


@router.patch("/prospects/{prospect_id}")
def update_prospect(
    prospect_id: uuid.UUID, req: StatusIn, db: Session = Depends(get_tenant_db)
) -> dict:
    if req.status not in ("new", "saved", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be new|saved|dismissed")
    p = db.scalar(
        select(Prospect).where(Prospect.tenant_id == _tenant(), Prospect.id == prospect_id)
    )
    if p is None:
        raise HTTPException(status_code=404, detail="prospect not found")
    p.status = req.status
    db.commit()
    return {"prospect": _prospect_out(p)}


@router.post("/prospects/{prospect_id}/save")
def save_to_network(prospect_id: uuid.UUID, db: Session = Depends(get_tenant_db)) -> dict:
    try:
        person = discover.promote_to_network(db, _tenant(), prospect_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    p = db.scalar(select(Prospect).where(Prospect.id == prospect_id))
    return {"person_id": str(person.id), "prospect": _prospect_out(p)}
