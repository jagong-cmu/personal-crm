"""Manual sync triggers. Slice 0 ships 'contacts'; others 501 until built."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_tenant_db
from app.services.connectors import contacts

router = APIRouter(prefix="/sync", tags=["sync"])

_CONNECTORS = {"contacts": contacts.sync}


@router.post("/{source}")
def run_sync(source: str, db: Session = Depends(get_tenant_db)) -> dict:
    fn = _CONNECTORS.get(source)
    if fn is None:
        raise HTTPException(
            status_code=501,
            detail=f"Source '{source}' not built yet. Available: {sorted(_CONNECTORS)}",
        )
    try:
        result = fn(db, get_settings().tenant_uuid)
    except RuntimeError as exc:  # e.g. not connected
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": source, "result": asdict(result)}
