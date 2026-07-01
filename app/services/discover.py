"""Discover orchestrator: profile CRUD, discovery run, and save-to-network promotion.

The discovery run is deliberately synchronous with a hard candidate cap — it's a
user-triggered, single-tenant action behind a spinner that hits two paid APIs, so a
capped run keeps cost/latency bounded. All orchestration lives here (not in the router)
so it stays testable with a fake provider and so a future worker/job upgrade is trivial.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Person, PersonSource, Prospect, UserProfile
from app.services import relation
from app.services.connectors import base
from app.services.connectors.base import NormalizedRecord
from app.services.embedding import embed_documents
from app.services.profile_capture import UserProfileData
from app.services.providers.base import DiscoveryProvider
from app.services.scoring import (
    company_match,
    cosine,
    geography_match,
    school_match,
    score_prospect,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "contactgen"
_DEFAULT_MAX = 8
_HARD_MAX = 15


@dataclass
class DiscoverySummary:
    created: int = 0
    skipped_dupes: int = 0
    truncated: int = 0  # candidates dropped by the cap
    contactless: int = 0  # prospects saved with no email/phone
    llm_skipped: int = 0  # prospects saved without a relation summary


# --------------------------------------------------------------------------------------
# Profile CRUD
# --------------------------------------------------------------------------------------


def get_profile(db: Session, tenant_id: uuid.UUID) -> UserProfile | None:
    return db.scalar(select(UserProfile).where(UserProfile.tenant_id == tenant_id))


def save_profile(db: Session, tenant_id: uuid.UUID, data: UserProfileData) -> UserProfile:
    """Upsert the tenant's single profile row (uq_user_profile_tenant)."""
    values = {
        "tenant_id": tenant_id,
        "display_name": data.display_name,
        "headline": data.headline,
        "location": data.location,
        "schools": data.schools,
        "companies": data.companies,
        "skills": data.skills,
        "about": data.about,
        "raw": data.raw,
    }
    stmt = (
        pg_insert(UserProfile)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_user_profile_tenant",
            set_={k: v for k, v in values.items() if k != "tenant_id"},
        )
    )
    db.execute(stmt)
    db.commit()
    return get_profile(db, tenant_id)


def profile_text(p: UserProfile) -> str:
    """The text embedded to represent the user's interests for semantic matching."""
    parts = [
        p.headline,
        p.about,
        " ".join(p.schools or []),
        " ".join(p.companies or []),
        " ".join(p.skills or []),
    ]
    return " — ".join(x for x in parts if x) or (p.display_name or "")


def _profile_data(p: UserProfile) -> UserProfileData:
    return UserProfileData(
        display_name=p.display_name,
        headline=p.headline,
        location=p.location,
        schools=list(p.schools or []),
        companies=list(p.companies or []),
        skills=list(p.skills or []),
        about=p.about,
    )


# --------------------------------------------------------------------------------------
# Discovery run
# --------------------------------------------------------------------------------------


def _dedupe_key(name: str, company: str | None, source_url: str | None) -> str:
    """Stable idempotency key: normalized source URL if present, else sha256(name|company)."""
    if source_url:
        return source_url.strip().lower().rstrip("/")
    digest = hashlib.sha256(f"{name}|{company or ''}".lower().encode()).hexdigest()
    return f"cg:{digest[:16]}"


def _existing_keys(db: Session, tenant_id: uuid.UUID) -> set[str]:
    """Keys already represented — existing prospects + already-known people — so a run
    never re-lists someone. People are keyed the same way (name|company)."""
    keys: set[str] = set()
    for (dk,) in db.execute(
        select(Prospect.dedupe_key).where(Prospect.tenant_id == tenant_id)
    ):
        keys.add(dk)
    for name, company in db.execute(
        select(Person.display_name, Person.company).where(
            Person.tenant_id == tenant_id, Person.merged_into_id.is_(None)
        )
    ):
        if name:
            keys.add(_dedupe_key(name, company, None))
    return keys


def run_discovery(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    provider: DiscoveryProvider,
    max_candidates: int = _DEFAULT_MAX,
) -> DiscoverySummary:
    """End to end: profile → Brave discovery → Hunter email → score → persist prospects.

    Raises ValueError if no profile exists yet. Per-candidate failures are isolated and
    logged (the run continues), mirroring the ingest pipeline's per-record isolation.
    """
    cap = max(1, min(int(max_candidates), _HARD_MAX))
    profile = get_profile(db, tenant_id)
    if profile is None:
        raise ValueError("Capture your profile first, then run discovery.")

    data = _profile_data(profile)

    raw_candidates = provider.search_people(data, limit=cap * 2)
    candidates = raw_candidates[:cap]
    summary = DiscoverySummary(truncated=max(0, len(raw_candidates) - len(candidates)))
    if summary.truncated:
        logger.info("discovery: capped %d candidates to %d", len(raw_candidates), cap)

    existing = _existing_keys(db, tenant_id)

    # Filter dupes first, then embed the profile + all fresh snippets in ONE batched
    # Voyage call (with backoff) rather than one embed_query per candidate — the latter
    # trips the free-tier rate limit and drops candidates.
    fresh: list[tuple[str, object]] = []
    for cand in candidates:
        key = _dedupe_key(cand.name, cand.company, cand.source_url)
        if key in existing:
            summary.skipped_dupes += 1
            continue
        existing.add(key)
        fresh.append((key, cand))

    if not fresh:
        db.commit()
        return summary

    texts = [profile_text(profile)] + [c.snippet or c.name for _, c in fresh]
    vectors = embed_documents(texts)
    user_vec, cand_vecs = vectors[0], vectors[1:]

    for (key, cand), cand_vec in zip(fresh, cand_vecs):
        try:
            _process_candidate(db, tenant_id, cand, key, user_vec, cand_vec, data, provider, summary)
        except Exception as exc:  # noqa: BLE001 — isolate one bad candidate
            logger.warning("discovery: candidate %r failed (%s); skipping", cand.name, exc)

    db.commit()
    logger.info(
        "discovery done: created=%d dupes=%d truncated=%d contactless=%d llm_skipped=%d",
        summary.created, summary.skipped_dupes, summary.truncated,
        summary.contactless, summary.llm_skipped,
    )
    return summary


def _process_candidate(db, tenant_id, cand, key, user_vec, cand_vec, data, provider, summary) -> None:
    # Contact info is provider-sourced ONLY. Hunter needs a company anchor to find a
    # work email; without one we keep any phone the discovery result carried, else the
    # prospect is contactless (still saved + scored, just flagged).
    email = provider.find_email(cand.name, cand.company).email if cand.company else None
    phone = cand.phone
    contactless = not email and not phone

    # Brave gives us noisy prose, not clean fields, so the exact-field match often misses
    # a real overlap that IS mentioned in the result text. Fall back to scanning the full
    # title+company+location+snippet "haystack" so geography/school/company still fire
    # (these are the user's stated top-priority signals).
    hay = " ".join(filter(None, [cand.title, cand.company, cand.location, cand.snippet]))
    result = score_prospect(
        geography=max(
            geography_match(data.location, cand.location),
            geography_match(data.location, hay),
        ),
        school=max(school_match(data.schools, cand.school), school_match(data.schools, hay)),
        company=max(company_match(data.companies, cand.company), company_match(data.companies, hay)),
        interest=cosine(user_vec, cand_vec),
    )

    summary_text = relation.synthesize(data, cand, result)
    if summary_text is None:
        summary.llm_skipped += 1
    if contactless:
        summary.contactless += 1

    breakdown = result.as_breakdown()
    breakdown["contactless"] = contactless

    stmt = (
        pg_insert(Prospect)
        .values(
            tenant_id=tenant_id,
            name=cand.name,
            email=email,
            phone=phone,
            company=cand.company,
            title=cand.title,
            location=cand.location,
            school=cand.school,
            source_url=cand.source_url,
            score=result.score,
            score_breakdown=breakdown,
            relation_summary=summary_text,
            status="new",
            dedupe_key=key,
            raw={"candidate": cand.raw},
        )
        .on_conflict_do_nothing(constraint="uq_prospect_dedupe")
    )
    db.execute(stmt)
    summary.created += 1


# --------------------------------------------------------------------------------------
# Save to network (promote a prospect into the real people table)
# --------------------------------------------------------------------------------------


def promote_to_network(db: Session, tenant_id: uuid.UUID, prospect_id: uuid.UUID) -> Person:
    """Promote a prospect into ``people`` via the shared ingest tail, then mark it saved.

    Reuses base.ingest so the prospect flows through the same resolve→upsert→embed path
    as every other source and dedupes against existing people.
    """
    prospect = db.scalar(
        select(Prospect).where(Prospect.tenant_id == tenant_id, Prospect.id == prospect_id)
    )
    if prospect is None:
        raise ValueError("Prospect not found.")

    rec = NormalizedRecord(
        source_type=SOURCE_TYPE,
        source_record_id=prospect.dedupe_key,
        display_name=prospect.name,
        primary_email=prospect.email,
        company=prospect.company,
        title=prospect.title,
        text=prospect.relation_summary
        or f"{prospect.name} — {prospect.title or ''} at {prospect.company or ''}".strip(),
        raw={
            "source_url": prospect.source_url,
            "score": prospect.score,
            "phone": prospect.phone,
        },
    )
    base.ingest(db, tenant_id, [rec])

    person_id = db.scalar(
        select(PersonSource.person_id).where(
            PersonSource.tenant_id == tenant_id,
            PersonSource.source_type == SOURCE_TYPE,
            PersonSource.source_record_id == prospect.dedupe_key,
        )
    )
    person = db.scalar(select(Person).where(Person.id == person_id)) if person_id else None

    prospect.status = "saved"
    prospect.promoted_person_id = person_id
    db.commit()
    if person is None:
        # Resolution dropped the record (e.g. non-human filter) — surface a clear error.
        raise ValueError("Could not promote this prospect (entity resolution skipped it).")
    return person
