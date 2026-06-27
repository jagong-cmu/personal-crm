"""Manual sync triggers.

  POST /sync/{source}   — contacts | gmail | calendar  (OAuth-backed pull)
  POST /sync/linkedin   — multipart upload of the LinkedIn Connections.csv export

Each OAuth source owns its own fetch loop; LinkedIn is a file import (no API).
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_tenant_db
from app.services.connectors import calendar, contacts, gmail, linkedin

router = APIRouter(prefix="/sync", tags=["sync"])

_CONNECTORS = {
    "contacts": contacts.sync,
    "gmail": gmail.sync,
    "calendar": calendar.sync,
}


@router.post("/linkedin")
async def sync_linkedin(
    file: UploadFile = File(...), db: Session = Depends(get_tenant_db)
) -> dict:
    """Import a LinkedIn Connections.csv export. Idempotent on re-import."""
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")  # export is UTF-8, often with a BOM
    except UnicodeDecodeError:
        content = raw.decode("latin-1", errors="replace")
    result = linkedin.sync_csv(db, get_settings().tenant_uuid, content)
    return {"source": "linkedin", "result": asdict(result)}


@router.post("/{source}")
def run_sync(source: str, db: Session = Depends(get_tenant_db)) -> dict:
    fn = _CONNECTORS.get(source)
    if fn is None:
        raise HTTPException(
            status_code=501,
            detail=(
                f"Source '{source}' not available. OAuth sources: {sorted(_CONNECTORS)}; "
                "LinkedIn uses POST /sync/linkedin (file upload)."
            ),
        )
    try:
        result = fn(db, get_settings().tenant_uuid)
    except RuntimeError as exc:  # e.g. not connected
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": source, "result": asdict(result)}
