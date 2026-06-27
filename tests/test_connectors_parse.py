"""Offline unit tests for the connector parsers (T15).

All pure: no network, no DB. Cover Gmail header parsing (participant fan-out, self
filtering, sent vs received), Calendar attendee parsing (self/resource skip), and the
LinkedIn CSV parser (notes preamble, blank emails, idempotent stable ids).
"""
from __future__ import annotations

from app.services.connectors import gmail, linkedin
from app.services.connectors.calendar import parse_event
from app.services.connectors.base import KEY_SEP


# --------------------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------------------


def _msg(headers: dict, msg_id="m1", internal="1700000000000") -> dict:
    return {
        "id": msg_id,
        "internalDate": internal,
        "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]},
    }


def test_gmail_received_fans_out_participants_and_skips_self():
    msg = _msg(
        {
            "From": "Sarah Chen <sarah@stripe.com>",
            "To": "me@example.com, Raj <raj@stripe.com>",
            "Subject": "Integration review",
        }
    )
    records, specs, occurred, subject = gmail.parse_message(msg, {"me@example.com"})
    emails = {r.primary_email for r in records}
    assert emails == {"sarah@stripe.com", "raj@stripe.com"}  # self dropped
    assert subject == "Integration review"
    assert occurred is not None
    assert all(s["interaction_type"] == "email_received" for s in specs)
    # source_record_id is message-id-prefixed for deletion reconciliation
    assert all(r.source_record_id.startswith(f"m1{KEY_SEP}") for r in records)


def test_gmail_sent_when_user_is_sender():
    msg = _msg({"From": "me@example.com", "To": "Sarah <sarah@stripe.com>", "Subject": "Hi"})
    records, specs, _, _ = gmail.parse_message(msg, {"me@example.com"})
    assert {r.primary_email for r in records} == {"sarah@stripe.com"}
    assert specs[0]["interaction_type"] == "email_sent"


def test_gmail_dedupes_repeated_address():
    msg = _msg({"From": "Sarah <sarah@stripe.com>", "To": "sarah@stripe.com", "Subject": "x"})
    records, _, _, _ = gmail.parse_message(msg, set())
    assert len(records) == 1


def test_gmail_no_body_text_embedded():
    # Privacy: embedded text must be subject + participant only — never any body field.
    msg = _msg({"From": "Sarah <sarah@stripe.com>", "Subject": "Q3 planning"})
    records, _, _, _ = gmail.parse_message(msg, set())
    assert records[0].text == "Email 'Q3 planning' with Sarah"


# --------------------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------------------


def test_calendar_skips_self_and_resources():
    event = {
        "id": "e1",
        "summary": "Quarterly sync",
        "start": {"dateTime": "2026-06-01T10:00:00Z"},
        "attendees": [
            {"email": "me@example.com", "self": True},
            {"email": "sarah@stripe.com", "displayName": "Sarah Chen"},
            {"email": "room-a@resource.calendar.google.com", "resource": True},
        ],
    }
    records, specs, occurred, title = parse_event(event)
    assert {r.primary_email for r in records} == {"sarah@stripe.com"}
    assert title == "Quarterly sync"
    assert occurred is not None and occurred.year == 2026
    assert records[0].text == "Meeting 'Quarterly sync' with Sarah Chen"


def test_calendar_date_only_event():
    event = {"id": "e2", "summary": "All day", "start": {"date": "2026-06-26"},
             "attendees": [{"email": "x@y.com"}]}
    _, _, occurred, _ = parse_event(event)
    assert occurred is not None and occurred.day == 26


# --------------------------------------------------------------------------------------
# LinkedIn CSV
# --------------------------------------------------------------------------------------

_CSV = (
    "Notes:\n"
    '"When exporting your connection data, you may notice..."\n'
    "\n"
    "First Name,Last Name,Email Address,Company,Position,Connected On\n"
    "Sarah,Chen,,Stripe,Staff Engineer,01 Jan 2024\n"
    "Raj,Patel,raj@stripe.com,Stripe,Product Manager,02 Jan 2024\n"
    ",,,,,\n"  # blank trailing row
)


def test_linkedin_parses_after_preamble():
    records = linkedin.parse_connections_csv(_CSV)
    assert len(records) == 2
    sarah = next(r for r in records if r.display_name == "Sarah Chen")
    assert sarah.primary_email is None  # LinkedIn usually omits email
    assert sarah.company == "Stripe"
    assert sarah.title == "Staff Engineer"
    assert "Staff Engineer" in sarah.text and "Stripe" in sarah.text


def test_linkedin_stable_id_is_deterministic():
    a = linkedin.parse_connections_csv(_CSV)
    b = linkedin.parse_connections_csv(_CSV)
    assert [r.source_record_id for r in a] == [r.source_record_id for r in b]


def test_linkedin_empty_when_no_header():
    assert linkedin.parse_connections_csv("garbage,with,no,header\n1,2,3,4") == []
