"""Read-only projections that drive the web UI's rail + Sources page.

  GET /stats    — indexed people / embedded chunk counts (rail stat, empty-state gate)
  GET /sources  — connection + freshness status per source

Both are pure reads over existing tables; no new persistence. Source *actions*
(connect, sync, import) reuse the existing /auth and /sync endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_tenant_db
from app.models import (
    EmbeddingChunk,
    OAuthCredential,
    Person,
    PersonSource,
    SyncState,
)

router = APIRouter(tags=["meta"])

# Presentation metadata. `auth="google"` sources connect via the OAuth flow;
# linkedin is a CSV upload (POST /sync/linkedin).
_SOURCES = [
    {"id": "contacts", "name": "Google Contacts", "auth": "google", "kind": "oauth"},
    {"id": "gmail", "name": "Gmail", "auth": "google", "kind": "oauth", "note": "metadata only · headers"},
    {"id": "calendar", "name": "Calendar", "auth": "google", "kind": "oauth", "note": "not connected"},
    {"id": "linkedin", "name": "LinkedIn", "auth": None, "kind": "upload", "note": "CSV import"},
]


def _tenant():
    return get_settings().tenant_uuid


def _ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


@router.get("/stats")
def stats(db: Session = Depends(get_tenant_db)) -> dict:
    t = _tenant()
    people = db.scalar(
        select(func.count())
        .select_from(Person)
        .where(Person.tenant_id == t, Person.merged_into_id.is_(None))
    )
    chunks = db.scalar(
        select(func.count()).select_from(EmbeddingChunk).where(EmbeddingChunk.tenant_id == t)
    )
    return {"people": people or 0, "chunks": chunks or 0}


@router.get("/sources")
def sources(db: Session = Depends(get_tenant_db)) -> dict:
    t = _tenant()
    cred = db.scalar(
        select(OAuthCredential).where(
            OAuthCredential.tenant_id == t, OAuthCredential.provider == "google"
        )
    )
    google_connected = bool(cred and cred.encrypted_refresh_token)
    needs_reconnect = bool(
        google_connected and cred.expires_at and cred.expires_at < datetime.now(timezone.utc)
    )

    counts = dict(
        db.execute(
            select(PersonSource.source_type, func.count(func.distinct(PersonSource.person_id)))
            .where(PersonSource.tenant_id == t)
            .group_by(PersonSource.source_type)
        ).all()
    )
    synced = dict(
        db.execute(
            select(SyncState.source_type, SyncState.last_synced_at).where(SyncState.tenant_id == t)
        ).all()
    )

    out = []
    for s in _SOURCES:
        is_google = s["auth"] == "google"
        n = counts.get(s["id"], 0)
        connected = google_connected if is_google else n > 0
        if connected and is_google and needs_reconnect:
            status = "reconnect"
        elif connected:
            status = "on"
        else:
            status = "off"

        last = synced.get(s["id"])
        if status == "reconnect":
            meta = "access expired · reconnect"
        elif status == "on" and n:
            meta = f"{n:,} people" + (f" · synced {_ago(last)}" if last else "")
        elif status == "on":
            meta = "connected · run a sync"
        else:
            meta = s.get("note", "not connected")

        action = (
            "re-sync"
            if status == "on"
            else "reconnect"
            if status == "reconnect"
            else "import"
            if s["kind"] == "upload"
            else "connect"
        )
        out.append(
            {
                "id": s["id"],
                "name": s["name"],
                "status": status,
                "people": n,
                "meta": meta,
                "kind": s["kind"],
                "action": action,
            }
        )

    return {
        "google_connected": google_connected,
        "needs_reconnect": needs_reconnect,
        "sources": out,
    }
