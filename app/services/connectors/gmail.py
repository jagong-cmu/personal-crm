"""Gmail connector (T11 + deletions T8).

PRIVACY (locked decision): scope is ``gmail.metadata`` — HEADERS ONLY. Message bodies
and snippets are never fetched and never leave the machine. We embed only the network
signal: subject + the other participant's name/email. No body text reaches Voyage.

Each message fans out to one row per external human participant (From/To/Cc, minus the
user's own addresses and non-human senders, which entity resolution drops). For each
participant we:
  * ingest a NormalizedRecord  -> resolve/create the person + embed "Email '<subj>' with X"
  * record an Interaction      -> timeline row (occurred_at from internalDate)

INCREMENTAL SYNC + DELETIONS
  * cursor = last processed Gmail ``historyId`` (persisted in sync_state).
  * No cursor -> full sync (messages.list), then store the mailbox's current historyId.
  * Cursor present -> history.list(startHistoryId=cursor): messagesAdded are re-ingested,
    messagesDeleted are reconciled via apply_deletions (soft-delete interaction + drop
    embedding). On HTTP 404 (historyId too old to serve) we clear the cursor and fall
    back to a full resync, which reconciles absent records implicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.services.connectors import base
from app.services.connectors.base import IngestResult, NormalizedRecord, participant_key
from app.services.google_oauth import load_credentials

SOURCE_TYPE = "gmail"
_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date"]
_MAX_FULL_SYNC_MESSAGES = 500  # dev cap; polling keeps it current incrementally after


# --------------------------------------------------------------------------------------
# Pure header parsing (no network / DB — unit-testable offline)
# --------------------------------------------------------------------------------------


def _parse_addresses(value: str | None) -> list[tuple[str | None, str]]:
    """Parse an address header into (display_name, email) pairs. Pure."""
    if not value:
        return []
    from email.utils import getaddresses

    out: list[tuple[str | None, str]] = []
    for name, addr in getaddresses([value]):
        addr = (addr or "").strip()
        if not addr or "@" not in addr:
            continue
        out.append((name.strip() or None, addr))
    return out


def _header_map(message: dict) -> dict[str, str]:
    headers = (message.get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in headers}


def _occurred_at(message: dict) -> datetime | None:
    ms = message.get("internalDate")
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_message(
    message: dict, own_emails: set[str]
) -> tuple[list[NormalizedRecord], list[dict], datetime | None, str | None]:
    """Turn one metadata message into per-participant (records, interactions).

    Returns (records, interaction_specs, occurred_at, subject). Each interaction_spec is
    a dict {source_record_id, email} the caller binds to the resolved person. Pure.
    """
    msg_id = message.get("id", "")
    hdrs = _header_map(message)
    subject = hdrs.get("subject") or "(no subject)"
    occurred = _occurred_at(message)

    own = {e.lower() for e in own_emails}
    from_addrs = _parse_addresses(hdrs.get("from"))
    user_is_sender = any(addr.lower() in own for _, addr in from_addrs)
    interaction_type = "email_sent" if user_is_sender else "email_received"

    seen: set[str] = set()
    records: list[NormalizedRecord] = []
    specs: list[dict] = []
    for name, addr in [*from_addrs, *_parse_addresses(hdrs.get("to")), *_parse_addresses(hdrs.get("cc"))]:
        key = addr.lower()
        if key in own or key in seen:
            continue
        seen.add(key)
        srid = participant_key(msg_id, key)
        display = name or addr
        records.append(
            NormalizedRecord(
                source_type=SOURCE_TYPE,
                source_record_id=srid,
                display_name=name,
                primary_email=addr,
                text=f"Email '{subject}' with {display}",
                raw={"message_id": msg_id, "header_name": name, "subject": subject},
            )
        )
        specs.append({"source_record_id": srid, "email": addr, "interaction_type": interaction_type})

    return records, specs, occurred, subject


# --------------------------------------------------------------------------------------
# Network helpers
# --------------------------------------------------------------------------------------


def _own_emails(service) -> set[str]:
    prof = service.users().getProfile(userId="me").execute()
    addr = prof.get("emailAddress")
    return {addr.lower()} if addr else set()


def _current_history_id(service) -> str | None:
    return service.users().getProfile(userId="me").execute().get("historyId")


def _get_metadata(service, msg_id: str) -> dict:
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata", metadataHeaders=_METADATA_HEADERS)
        .execute()
    )


def _list_full_message_ids(service) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < _MAX_FULL_SYNC_MESSAGES:
        resp = (
            service.users()
            .messages()
            .list(userId="me", maxResults=100, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:_MAX_FULL_SYNC_MESSAGES]


def _history_changes(service, start_history_id: str) -> tuple[list[str], list[str], str | None]:
    """Return (added_message_ids, deleted_message_ids, new_history_id) since the cursor."""
    added: list[str] = []
    deleted: list[str] = []
    new_hid: str | None = None
    page_token: str | None = None
    while True:
        resp = (
            service.users()
            .history()
            .list(userId="me", startHistoryId=start_history_id, pageToken=page_token)
            .execute()
        )
        new_hid = resp.get("historyId", new_hid)
        for h in resp.get("history", []):
            for a in h.get("messagesAdded", []):
                added.append(a["message"]["id"])
            for d in h.get("messagesDeleted", []):
                deleted.append(d["message"]["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return list(dict.fromkeys(added)), list(dict.fromkeys(deleted)), new_hid


# --------------------------------------------------------------------------------------
# Ingest one batch of message ids
# --------------------------------------------------------------------------------------


def _ingest_message_ids(
    db: Session, tenant_id, service, msg_ids: list[str], own: set[str]
) -> IngestResult:
    all_records: list[NormalizedRecord] = []
    # (source_record_id -> (email, interaction_type, occurred_at, subject, msg_id))
    interaction_index: dict[str, dict] = {}
    for msg_id in msg_ids:
        try:
            msg = _get_metadata(service, msg_id)
        except HttpError:
            continue  # message vanished between list and get; skip
        records, specs, occurred, subject = parse_message(msg, own)
        all_records.extend(records)
        for spec in specs:
            interaction_index[spec["source_record_id"]] = {
                **spec,
                "occurred_at": occurred,
                "subject": subject,
                "msg_id": msg_id,
            }

    result = base.ingest(db, tenant_id, all_records)

    # Record interactions for the people the ingest resolved (best-effort timeline).
    from app.models import Person, PersonSource
    from sqlalchemy import select

    for srid, info in interaction_index.items():
        person_id = db.scalar(
            select(PersonSource.person_id).where(
                PersonSource.tenant_id == tenant_id,
                PersonSource.source_type == SOURCE_TYPE,
                PersonSource.source_record_id == srid,
            )
        )
        if person_id is None:
            continue  # record was dropped (non-human) or failed; no timeline row
        person = db.get(Person, person_id)
        base.record_interaction(
            db,
            tenant_id,
            person=person,
            source_type=SOURCE_TYPE,
            source_record_id=srid,
            interaction_type=info["interaction_type"],
            occurred_at=info["occurred_at"],
            subject=info["subject"],
            external_id=info["msg_id"],
        )
    db.commit()
    return result


def sync(db: Session, tenant_id) -> IngestResult:
    creds = load_credentials(db, tenant_id)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    own = _own_emails(service)

    cursor = base.get_cursor(db, tenant_id, SOURCE_TYPE)

    if cursor:
        try:
            added, deleted, new_hid = _history_changes(service, cursor)
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == 404:
                base.set_cursor(db, tenant_id, SOURCE_TYPE, None)  # invalidate -> full resync
                cursor = None
            else:
                raise
        else:
            result = _ingest_message_ids(db, tenant_id, service, added, own)
            result.deletions = base.apply_deletions(db, tenant_id, SOURCE_TYPE, deleted)
            if new_hid:
                base.set_cursor(db, tenant_id, SOURCE_TYPE, new_hid)
            return result

    # Full sync (no cursor or after 404 invalidation).
    hid = _current_history_id(service)
    msg_ids = _list_full_message_ids(service)
    result = _ingest_message_ids(db, tenant_id, service, msg_ids, own)
    if hid:
        base.set_cursor(db, tenant_id, SOURCE_TYPE, hid)
    return result
