"""Entity-resolution engine (T4).

Decides whether an incoming NormalizedRecord maps to an existing Person or creates a
new one. The matching *logic* is split into PURE, DB-free functions so it can be unit
tested offline; only ``resolve()`` touches the database.

Authoritative resolution order (decisions A4 / merge-persistence):

    1. alias hit?                     -> return the aliased person (manual/auto merges persist)
    2. exact normalized email match?  -> bind to that person
    3. non-human / list?              -> drop (no-op)
    4. has email, no company          -> PROVISIONAL person (email-keyed only; NO name fuzzy)
    5. has company                    -> fuzzy(name+company); >= 0.85 merge, else NEW + manual review

`person_aliases` is consulted FIRST (step 1). Un-merge = delete the alias row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Person, PersonAlias

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Fuzzy name+company score at/above which an incoming record is auto-merged into an
#: existing person. Below it, a NEW person is created and flagged for manual review.
FUZZY_MERGE_THRESHOLD = 0.85

#: Weights for the blended name+company similarity (must sum to 1.0). Name dominates
#: because two different people at the same company must NOT collapse together, while
#: the company term still pulls apart same-name people at different companies.
_NAME_WEIGHT = 0.6
_COMPANY_WEIGHT = 0.4

# Local-part substrings / prefixes that mark an address as non-human (automated,
# bounce, calendar, or mailing-list traffic). Matched against the lower-cased local part.
_NON_HUMAN_SUBSTRINGS = (
    "no-reply",
    "noreply",
    "no.reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
    "mailer-daemon",
    "mailerdaemon",
    "postmaster",
    "bounce",  # bounce, bounces, *-bounces
    "notifications",
    "notification",
    "automated",
    "auto-confirm",
    "listserv",
    "majordomo",
    "+unsubscribe",
    "-unsubscribe",
    "-request",  # mailing-list -request control address
)

# Local-part prefixes that mark non-human/calendar/list senders.
_NON_HUMAN_PREFIXES = (
    "calendar-",
    "calendar+",
    "calendar.",
)

# Exact local parts that are non-human.
_NON_HUMAN_EXACT = (
    "calendar",
    "mailer-daemon",
    "postmaster",
)

# Legal-entity suffixes stripped before comparing company names.
_COMPANY_SUFFIXES = (
    "inc",
    "incorporated",
    "llc",
    "l.l.c",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "gmbh",
    "plc",
    "lp",
    "llp",
    "sa",
    "ag",
)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


# --------------------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------------------


@dataclass
class ResolutionResult:
    """Outcome of resolving one record.

    person:        the canonical Person, or None when the record was dropped.
    created:       True when a brand-new Person row was created.
    confidence:    match confidence in [0,1]; None for exact-email / alias binds.
    needs_review:  True when a NEW person was created on a weak fuzzy signal.
    provisional:   True for email-only (no company) people resolved by email alone.
    dropped:       True when the record was filtered (non-human/list).
    """

    person: Person | None
    created: bool
    confidence: float | None = None
    needs_review: bool = False
    provisional: bool = False
    dropped: bool = False


# --------------------------------------------------------------------------------------
# Pure helpers (NO database access — unit-testable offline)
# --------------------------------------------------------------------------------------


def normalize_email(email: str | None) -> str | None:
    """Lower-case + trim an email; return None for empty/None."""
    if not email:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


def _split_local_domain(email: str) -> tuple[str, str]:
    local, _, domain = email.partition("@")
    return local, domain


def is_non_human(
    email: str | None,
    display_name: str | None,
    own_emails: set[str] | None = None,
) -> bool:
    """True when an address should be dropped rather than resolved to a person.

    Catches no-reply / do-not-reply, bounce + mailer-daemon, ``calendar-*``, mailing
    lists (listserv/majordomo/*-request/unsubscribe), notification senders, and the
    user's own aliases (``own_emails``, passed by ``resolve``). Pure: no DB access.
    Records with no email are NOT non-human here (they may be name-only people).
    """
    norm = normalize_email(email)
    if norm is None:
        return False

    if own_emails and norm in {normalize_email(e) for e in own_emails}:
        return True

    local, domain = _split_local_domain(norm)

    if local in _NON_HUMAN_EXACT:
        return True
    if any(local.startswith(p) for p in _NON_HUMAN_PREFIXES):
        return True
    if any(sub in local for sub in _NON_HUMAN_SUBSTRINGS):
        return True

    # Bounce/automated subdomains (e.g. bounce.example.com, email.marketing.foo.com).
    if domain.startswith(("bounce.", "bounces.", "mailer.")):
        return True

    return False


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = _PUNCT.sub(" ", name.lower())
    return _WS.sub(" ", s).strip()


def _normalize_company(company: str | None) -> str:
    if not company:
        return ""
    s = _PUNCT.sub(" ", company.lower())
    tokens = [t for t in _WS.sub(" ", s).strip().split(" ") if t]
    # Drop trailing legal suffixes (Acme Inc. == Acme).
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def name_company_similarity(
    a_name: str | None,
    a_company: str | None,
    b_name: str | None,
    b_company: str | None,
) -> float:
    """Blended name+company similarity in [0,1]. COMPANY-GATED.

    Returns 0.0 unless BOTH records carry a company (fuzzy(name+company) fires only
    when company is present — email-only participants never reach here).

    Scoring (after normalization — lower-cased, de-punctuated, legal suffixes stripped):
        name_sim    = SequenceMatcher ratio on normalized names
        company_sim = 1.0 if normalized companies are equal, else SequenceMatcher ratio
        score       = 0.6 * name_sim + 0.4 * company_sim

    Identical name+company -> 1.0. Same name, different company -> name carries 0.6 but
    the company term collapses, landing below the 0.85 merge threshold. Different name,
    same company stays well below threshold so two coworkers never merge.
    """
    a_comp = _normalize_company(a_company)
    b_comp = _normalize_company(b_company)
    if not a_comp or not b_comp:
        return 0.0  # company-gated: no fuzzy without company on both sides

    name_sim = _ratio(_normalize_name(a_name), _normalize_name(b_name))
    company_sim = 1.0 if a_comp == b_comp else _ratio(a_comp, b_comp)

    return _NAME_WEIGHT * name_sim + _COMPANY_WEIGHT * company_sim


# --------------------------------------------------------------------------------------
# DB-backed resolution
# --------------------------------------------------------------------------------------


def _create_person(db: Session, tenant_id, rec, email: str | None) -> Person:
    person = Person(
        tenant_id=tenant_id,
        display_name=rec.display_name,
        primary_email=email,
        company=rec.company,
        title=rec.title,
    )
    db.add(person)
    db.flush()  # assign id
    return person


def resolve(db: Session, tenant_id, rec, own_emails: set[str] | None = None) -> ResolutionResult:
    """Resolve one NormalizedRecord against the 5-step order. Touches the DB."""
    email = normalize_email(rec.primary_email)

    # 1. Alias hit — manual/auto merges persist and win over everything else.
    alias = db.scalar(
        select(PersonAlias).where(
            PersonAlias.tenant_id == tenant_id,
            PersonAlias.source_type == rec.source_type,
            PersonAlias.source_record_id == rec.source_record_id,
        )
    )
    if alias is not None:
        person = db.get(Person, alias.person_id)
        if person is not None:
            return ResolutionResult(person=person, created=False, confidence=None)

    # 2. Exact normalized-email match.
    if email:
        existing = db.scalar(
            select(Person).where(
                Person.tenant_id == tenant_id, Person.primary_email == email
            )
        )
        if existing is not None:
            return ResolutionResult(person=existing, created=False, confidence=None)

    # 3. Non-human / list -> drop.
    if is_non_human(email, rec.display_name, own_emails=own_emails):
        return ResolutionResult(person=None, created=False, dropped=True)

    # 4. Has email, no company -> PROVISIONAL person (email-keyed only; no name fuzzy).
    if email and not rec.company:
        person = _create_person(db, tenant_id, rec, email)
        return ResolutionResult(person=person, created=True, provisional=True)

    # 5. Has company -> fuzzy(name+company). >= threshold merge, else NEW + manual review.
    if rec.company:
        candidates = db.scalars(
            select(Person).where(
                Person.tenant_id == tenant_id, Person.company.is_not(None)
            )
        ).all()
        best_person: Person | None = None
        best_score = 0.0
        for cand in candidates:
            score = name_company_similarity(
                rec.display_name, rec.company, cand.display_name, cand.company
            )
            if score > best_score:
                best_score, best_person = score, cand

        if best_person is not None and best_score >= FUZZY_MERGE_THRESHOLD:
            return ResolutionResult(
                person=best_person, created=False, confidence=best_score
            )

        person = _create_person(db, tenant_id, rec, email)
        return ResolutionResult(
            person=person,
            created=True,
            confidence=best_score if best_person is not None else None,
            needs_review=True,
        )

    # Fallthrough: no email AND no company (name-only). Create a new person; it can't be
    # keyed for dedup, so flag it for review.
    person = _create_person(db, tenant_id, rec, email)
    return ResolutionResult(person=person, created=True, needs_review=True)
