"""Google OAuth endpoints.

Note: the brief lists POST /auth/google/start, but a browser consent flow needs a
GET redirect to be clickable. We expose GET /auth/google/start (302 -> Google) and
the GET callback Google redirects back to. Single tenant (DEFAULT_TENANT_ID) for now.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.services import google_oauth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/google", tags=["auth"])


@router.get("/start")
def start() -> RedirectResponse:
    url, _state = google_oauth.authorization_url()
    return RedirectResponse(url)


@router.get("/callback")
def callback(
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?auth_error={error}", status_code=303)
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    tenant_id = get_settings().tenant_uuid
    try:
        google_oauth.exchange_and_store(db, tenant_id, code)
    except Exception as exc:  # surface the real cause during local dev
        logger.exception("OAuth callback failed")
        raise HTTPException(
            status_code=500, detail=f"{type(exc).__name__}: {exc}"
        ) from exc
    # Land back in the web app; it shows a toast and prompts the first sync.
    return RedirectResponse("/?connected=1", status_code=303)
