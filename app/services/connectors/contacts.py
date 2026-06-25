"""Google Contacts connector (Slice 0's single source).

Owns its own fetch loop (pagination); hands NormalizedRecords to the shared tail.
Acceptance check (brief): running sync twice creates no duplicate people — held by
exact-email resolution + the person_sources unique key.
"""
from __future__ import annotations

from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.services.connectors.base import IngestResult, NormalizedRecord, ingest
from app.services.google_oauth import load_credentials

SOURCE_TYPE = "contacts"
_PERSON_FIELDS = "names,emailAddresses,organizations"


def _normalize(conn: dict) -> NormalizedRecord | None:
    resource = conn.get("resourceName", "")
    names = conn.get("names") or []
    emails = conn.get("emailAddresses") or []
    orgs = conn.get("organizations") or []

    display_name = names[0].get("displayName") if names else None
    primary_email = None
    for e in emails:
        if e.get("metadata", {}).get("primary") or primary_email is None:
            primary_email = e.get("value")
        if e.get("metadata", {}).get("primary"):
            break

    company = orgs[0].get("name") if orgs else None
    title = orgs[0].get("title") if orgs else None

    if not display_name and not primary_email:
        return None  # nothing to anchor on

    # Slice 0 embeds the network-graph signal, not email bodies.
    parts = [p for p in [display_name, title, company] if p]
    text = " — ".join(parts) if parts else (primary_email or "")

    return NormalizedRecord(
        source_type=SOURCE_TYPE,
        source_record_id=resource,
        display_name=display_name,
        primary_email=primary_email,
        company=company,
        title=title,
        text=text,
        raw={"names": names, "emailAddresses": emails, "organizations": orgs},
    )


def sync(db: Session, tenant_id) -> IngestResult:
    creds = load_credentials(db, tenant_id)
    service = build("people", "v1", credentials=creds, cache_discovery=False)

    records: list[NormalizedRecord] = []
    page_token: str | None = None
    while True:
        resp = (
            service.people()
            .connections()
            .list(
                resourceName="people/me",
                personFields=_PERSON_FIELDS,
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for conn in resp.get("connections", []):
            rec = _normalize(conn)
            if rec is not None:
                records.append(rec)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ingest(db, tenant_id, records)
