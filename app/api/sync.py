"""Manual sync triggers.

  POST /sync/{source}   — contacts | gmail | calendar  (OAuth-backed pull)
  POST /sync/linkedin   — multipart upload of the LinkedIn Connections.csv export

Each OAuth source owns its own fetch loop; LinkedIn is a file import (no API).
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_tenant_db
from app.services.connectors import calendar, contacts, gmail, linkedin

router = APIRouter(prefix="/sync", tags=["sync"])


def _google_http_detail(source: str, exc: HttpError) -> tuple[int, str]:
    """Map a Google API HttpError to (status, actionable message) for the client.

    The most common local-dev case is a 403 'accessNotConfigured' — the API for
    this source isn't enabled in the Google Cloud project — which otherwise 500s
    as an opaque 'Internal Server Error'. Surface what the user must actually do.
    """
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", 502)
    reason = (getattr(exc, "reason", "") or "").strip() or "Google API error"
    api = {"contacts": "People", "gmail": "Gmail", "calendar": "Calendar"}.get(source, source)
    if status == 403 and ("has not been used" in reason or "accessNotConfigured" in reason):
        return 502, (
            f"Google's {api} API isn't enabled for your Cloud project. Enable it in "
            "the Google Cloud console (APIs & Services → Library), wait ~1 minute, "
            "then retry the sync."
        )
    if status in (401, 403):
        return 502, (
            f"Google denied the {api} sync ({status}). Reconnect Google to refresh "
            f"access, then retry. ({reason})"
        )
    return 502, f"Google {api} sync failed ({status}): {reason}"

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
    except HttpError as exc:  # API disabled / auth / quota — give an actionable message
        status, detail = _google_http_detail(source, exc)
        raise HTTPException(status_code=status, detail=detail) from exc
    return {"source": source, "result": asdict(result)}
