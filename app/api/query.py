"""Natural-language query over the network. Returns answer + citations."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.services import rag

router = APIRouter(tags=["query"])


class QueryRequest(BaseModel):
    question: str


@router.post("/query")
def query(req: QueryRequest, db: Session = Depends(get_db)) -> dict:
    result = rag.query(db, get_settings().tenant_uuid, req.question)
    return {"answer": result.answer, "citations": [asdict(c) for c in result.citations]}
