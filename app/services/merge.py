"""Manual person merge / un-merge (T14).

A merge collapses a *loser* person into a *winner*:
  1. repoint the loser's person_sources, embedding_chunks, and interactions to the winner
  2. write a person_alias row per moved source record (decided_by='manual',
     merged_from_id=loser) so the merge STICKS across re-syncs — resolution (T4)
     consults aliases FIRST, so a re-synced record rebinds to the winner, never re-splits
  3. tombstone the loser (people.merged_into_id = winner); it is hidden from
     listings/retrieval but NOT deleted, so the merge is fully reversible

Un-merge reverses exactly the records this merge moved (identified by
person_aliases.merged_from_id), repointing them back to the loser and clearing the
tombstone + alias rows.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmbeddingChunk, Interaction, Person, PersonAlias, PersonSource


class MergeError(ValueError):
    """Raised for invalid merge/un-merge requests (caller maps to HTTP 400/404)."""


def _get_person(db: Session, tenant_id, person_id: uuid.UUID) -> Person:
    person = db.scalar(
        select(Person).where(Person.tenant_id == tenant_id, Person.id == person_id)
    )
    if person is None:
        raise MergeError(f"person {person_id} not found")
    return person


def _repoint(db: Session, tenant_id, src_person_id, dst_person_id) -> None:
    for model in (PersonSource, EmbeddingChunk, Interaction):
        db.execute(
            update(model)
            .where(model.tenant_id == tenant_id, model.person_id == src_person_id)
            .values(person_id=dst_person_id)
        )


def merge(db: Session, tenant_id, winner_id: uuid.UUID, loser_id: uuid.UUID) -> dict:
    if winner_id == loser_id:
        raise MergeError("cannot merge a person into themselves")
    winner = _get_person(db, tenant_id, winner_id)
    loser = _get_person(db, tenant_id, loser_id)
    if loser.merged_into_id is not None:
        raise MergeError(f"person {loser_id} is already merged")
    if winner.merged_into_id is not None:
        raise MergeError(f"winner {winner_id} is itself merged away")

    # Snapshot the loser's source records BEFORE repointing, to write sticky aliases.
    loser_sources = db.execute(
        select(PersonSource.source_type, PersonSource.source_record_id).where(
            PersonSource.tenant_id == tenant_id, PersonSource.person_id == loser_id
        )
    ).all()

    _repoint(db, tenant_id, loser_id, winner_id)

    for source_type, source_record_id in loser_sources:
        stmt = (
            pg_insert(PersonAlias)
            .values(
                tenant_id=tenant_id,
                source_type=source_type,
                source_record_id=source_record_id,
                person_id=winner_id,
                decided_by="manual",
                merged_from_id=loser_id,
            )
            .on_conflict_do_update(
                constraint="uq_person_alias",
                set_={
                    "person_id": winner_id,
                    "decided_by": "manual",
                    "merged_from_id": loser_id,
                },
            )
        )
        db.execute(stmt)

    loser.merged_into_id = winner_id
    db.commit()
    return {"winner_id": str(winner_id), "loser_id": str(loser_id), "moved_sources": len(loser_sources)}


def unmerge(db: Session, tenant_id, loser_id: uuid.UUID) -> dict:
    loser = _get_person(db, tenant_id, loser_id)
    if loser.merged_into_id is None:
        raise MergeError(f"person {loser_id} is not merged")

    aliases = db.scalars(
        select(PersonAlias).where(
            PersonAlias.tenant_id == tenant_id, PersonAlias.merged_from_id == loser_id
        )
    ).all()

    # Repoint exactly the records this merge moved back to the loser.
    for alias in aliases:
        for model in (PersonSource, EmbeddingChunk, Interaction):
            db.execute(
                update(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.source_type == alias.source_type,
                    model.source_record_id == alias.source_record_id,
                )
                .values(person_id=loser_id)
            )
    db.execute(
        delete(PersonAlias).where(
            PersonAlias.tenant_id == tenant_id, PersonAlias.merged_from_id == loser_id
        )
    )
    loser.merged_into_id = None
    db.commit()
    return {"unmerged_id": str(loser_id), "restored_sources": len(aliases)}
