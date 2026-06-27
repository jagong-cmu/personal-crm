"""Google Calendar connector (sources slice + deletions T8).

Each event fans out to one row per external human attendee (skipping the user
themselves and resource/room "attendees"). For each attendee we:
  * ingest a NormalizedRecord -> resolve/create the person + embed "Meeting '<title>' with X"
  * record an Interaction     -> timeline row (interaction_type=meeting, occurred_at=start)

INCREMENTAL SYNC + DELETIONS
  * cursor = Calendar ``syncToken`` (persisted in sync_state).
  * No cursor -> full sync (events.list paged), capturing the final nextSyncToken.
  * Cursor present -> events.list(syncToken=cursor): changed events re-ingest; events with
    ``status == "cancelled"`` are reconciled via apply_deletions. On HTTP 410 (syncToken
    expired) we clear the cursor and full-resync, which reconciles deletions implicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.models import Person, PersonSource
from sqlalchemy import select

from app.services.connectors import base
from app.services.connectors.base import IngestResult, NormalizedRecord, participant_key
from app.services.google_oauth import load_credentials

SOURCE_TYPE = "calendar"
_CALENDAR_ID = "primary"


# --------------------------------------------------------------------------------------
# Pure event parsing (no network / DB — unit-testable offline)
# --------------------------------------------------------------------------------------


def _event_start(event: dict) -> datetime | None:
    start = event.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        # date-only events ("2026-06-26") -> midnight UTC; dateTime is ISO-8601.
        if len(raw) == 10:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_event(event: dict) -> tuple[list[NormalizedRecord], list[dict], datetime | None, str]:
    """Turn one event into per-attendee (records, interaction_specs). Pure.

    Skips the user themselves (attendee.self), resource rooms (attendee.resource), and
    attendees without an email. Returns (records, specs, occurred_at, title).
    """
    event_id = event.get("id", "")
    title = event.get("summary") or "(no title)"
    occurred = _event_start(event)

    seen: set[str] = set()
    records: list[NormalizedRecord] = []
    specs: list[dict] = []
    for att in event.get("attendees") or []:
        if att.get("self") or att.get("resource"):
            continue
        email = (att.get("email") or "").strip()
        if not email or "@" not in email or email.lower() in seen:
            continue
        seen.add(email.lower())
        name = att.get("displayName")
        display = name or email
        srid = participant_key(event_id, email.lower())
        records.append(
            NormalizedRecord(
                source_type=SOURCE_TYPE,
                source_record_id=srid,
                display_name=name,
                primary_email=email,
                text=f"Meeting '{title}' with {display}",
                raw={"event_id": event_id, "title": title},
            )
        )
        specs.append({"source_record_id": srid, "email": email})

    return records, specs, occurred, title


# --------------------------------------------------------------------------------------
# Network paging
# --------------------------------------------------------------------------------------


def _list_events(service, *, sync_token: str | None) -> tuple[list[dict], str | None]:
    """Page through events. With sync_token -> incremental; without -> full. Returns
    (events, next_sync_token). Raises HttpError(410) if the sync_token expired."""
    events: list[dict] = []
    page_token: str | None = None
    next_sync_token: str | None = None
    while True:
        params = {"calendarId": _CALENDAR_ID, "maxResults": 250, "singleEvents": True}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = "2000-01-01T00:00:00Z"
        if page_token:
            params["pageToken"] = page_token
        resp = service.events().list(**params).execute()
        events.extend(resp.get("items", []))
        next_sync_token = resp.get("nextSyncToken", next_sync_token)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events, next_sync_token


# --------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------


def _ingest_events(db: Session, tenant_id, events: list[dict]) -> tuple[IngestResult, list[str]]:
    all_records: list[NormalizedRecord] = []
    interaction_index: dict[str, dict] = {}
    cancelled: list[str] = []
    for event in events:
        if event.get("status") == "cancelled":
            cancelled.append(event.get("id", ""))
            continue
        records, specs, occurred, title = parse_event(event)
        all_records.extend(records)
        for spec in specs:
            interaction_index[spec["source_record_id"]] = {
                "occurred_at": occurred,
                "subject": title,
                "event_id": event.get("id", ""),
            }

    result = base.ingest(db, tenant_id, all_records)

    for srid, info in interaction_index.items():
        person_id = db.scalar(
            select(PersonSource.person_id).where(
                PersonSource.tenant_id == tenant_id,
                PersonSource.source_type == SOURCE_TYPE,
                PersonSource.source_record_id == srid,
            )
        )
        if person_id is None:
            continue
        person = db.get(Person, person_id)
        base.record_interaction(
            db,
            tenant_id,
            person=person,
            source_type=SOURCE_TYPE,
            source_record_id=srid,
            interaction_type="meeting",
            occurred_at=info["occurred_at"],
            subject=info["subject"],
            external_id=info["event_id"],
        )
    db.commit()
    return result, cancelled


def sync(db: Session, tenant_id) -> IngestResult:
    creds = load_credentials(db, tenant_id)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    cursor = base.get_cursor(db, tenant_id, SOURCE_TYPE)
    try:
        events, next_token = _list_events(service, sync_token=cursor)
    except HttpError as exc:
        if cursor and getattr(exc, "resp", None) is not None and exc.resp.status == 410:
            base.set_cursor(db, tenant_id, SOURCE_TYPE, None)  # expired -> full resync
            events, next_token = _list_events(service, sync_token=None)
        else:
            raise

    result, cancelled = _ingest_events(db, tenant_id, events)
    result.deletions = base.apply_deletions(db, tenant_id, SOURCE_TYPE, cancelled)
    if next_token:
        base.set_cursor(db, tenant_id, SOURCE_TYPE, next_token)
    return result
