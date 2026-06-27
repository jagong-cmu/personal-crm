"""LinkedIn connector — official CSV export (merge slice).

LinkedIn has no usable connections API, so the supported path is the user's data
export: Settings → Data Privacy → Get a copy of your data → Connections → Connections.csv.

CSV SHAPE (and quirks this parser handles)
  * The file starts with a 2-3 line "Notes:" preamble BEFORE the real header row. We
    skip everything up to the line that starts with "First Name".
  * Columns: First Name, Last Name, Email Address, Company, Position, Connected On
    (older exports add a "URL" profile column; we use it as the stable id when present).
  * EMAIL IS USUALLY BLANK — LinkedIn only includes it when the connection opted in. So
    most rows carry name+company+title only. They resolve through the fuzzy(name+company)
    path (entity_resolution T4), merging against people already pulled from Contacts.

STABLE ID (for idempotent re-import): profile URL if present, else a content hash of
name+company. Re-importing the same export must not duplicate people — held by exact
email / fuzzy resolution + the person_sources unique key, same as every other source.
"""
from __future__ import annotations

import csv
import hashlib
import io

from sqlalchemy.orm import Session

from app.services.connectors import base
from app.services.connectors.base import IngestResult, NormalizedRecord

SOURCE_TYPE = "linkedin"


def _stable_id(url: str | None, full_name: str, company: str | None) -> str:
    if url:
        return url.strip()
    digest = hashlib.sha256(f"{full_name}|{company or ''}".lower().encode()).hexdigest()
    return f"linkedin:{digest[:16]}"


def _find_header_row(rows: list[list[str]]) -> int:
    """Index of the real header row (skips LinkedIn's Notes preamble). -1 if absent."""
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "first name":
            return i
    return -1


def parse_connections_csv(content: str) -> list[NormalizedRecord]:
    """Parse a LinkedIn Connections.csv into NormalizedRecords. Pure (no DB/network)."""
    rows = list(csv.reader(io.StringIO(content)))
    header_idx = _find_header_row(rows)
    if header_idx == -1:
        return []

    header = [h.strip().lower() for h in rows[header_idx]]
    col = {name: header.index(name) for name in header}

    def get(row: list[str], name: str) -> str | None:
        idx = col.get(name)
        if idx is None or idx >= len(row):
            return None
        val = row[idx].strip()
        return val or None

    records: list[NormalizedRecord] = []
    for row in rows[header_idx + 1 :]:
        if not any(cell.strip() for cell in row):
            continue
        first = get(row, "first name") or ""
        last = get(row, "last name") or ""
        full_name = f"{first} {last}".strip()
        company = get(row, "company")
        position = get(row, "position")
        email = get(row, "email address")
        url = get(row, "url")
        if not full_name and not email:
            continue

        # Embed the LinkedIn "headline" signal: name — position at company.
        parts = [p for p in [full_name or None, position, company] if p]
        text = " — ".join(parts) if parts else (email or "")

        records.append(
            NormalizedRecord(
                source_type=SOURCE_TYPE,
                source_record_id=_stable_id(url, full_name, company),
                display_name=full_name or None,
                primary_email=email,
                company=company,
                title=position,
                text=text,
                raw={"first": first, "last": last, "company": company, "position": position},
            )
        )
    return records


def sync_csv(db: Session, tenant_id, content: str) -> IngestResult:
    """Ingest a LinkedIn Connections.csv (passed as text). Idempotent on re-import."""
    records = parse_connections_csv(content)
    return base.ingest(db, tenant_id, records)
