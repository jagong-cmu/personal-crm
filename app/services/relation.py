"""Claude-synthesized "how this person relates to you" summary for a Discover prospect.

Grounded ONLY in the user's profile + the candidate's public snippet + the computed score
breakdown — no invented facts, and explicitly never contact info. Degrades to None on any
Anthropic error so a prospect is still saved (just without a written relationship note).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anthropic

from app.config import get_settings
from app.services import llm

if TYPE_CHECKING:
    from app.services.providers.base import PersonCandidate
    from app.services.profile_capture import UserProfileData
    from app.services.scoring import ScoreResult

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You explain how a newly-discovered person overlaps with the user's professional "
    "background, to help the user decide whether to reach out. Use ONLY the provided "
    "profile, the candidate's public snippet, and the overlap signals. Write 1-2 concise "
    "sentences naming the concrete shared points (school, location, company, interests). "
    "Do not invent facts. Never output email addresses or phone numbers. No preamble."
)


def _overlap_lines(score: "ScoreResult") -> str:
    labels = {
        "geography": "same location/region",
        "school": "shared school",
        "company": "shared company/industry",
        "interest": "overlapping interests",
    }
    hits = [labels[k] for k, v in score.features.items() if v > 0]
    return ", ".join(hits) if hits else "weak overlap"


def synthesize(
    profile: "UserProfileData", candidate: "PersonCandidate", score: "ScoreResult"
) -> str | None:
    """Return a short relationship blurb, or None if Claude is unavailable."""
    user_bits = [
        f"Name: {profile.display_name}" if profile.display_name else None,
        f"Headline: {profile.headline}" if profile.headline else None,
        f"Location: {profile.location}" if profile.location else None,
        f"Schools: {', '.join(profile.schools)}" if profile.schools else None,
        f"Companies: {', '.join(profile.companies)}" if profile.companies else None,
        f"Interests: {', '.join(profile.skills)}" if profile.skills else None,
    ]
    cand_bits = [
        f"Name: {candidate.name}",
        f"Title: {candidate.title}" if candidate.title else None,
        f"Company: {candidate.company}" if candidate.company else None,
        f"Location: {candidate.location}" if candidate.location else None,
        f"Snippet: {candidate.snippet}" if candidate.snippet else None,
    ]
    user_ctx = "\n".join(b for b in user_bits if b)
    cand_ctx = "\n".join(b for b in cand_bits if b)
    prompt = (
        f"USER PROFILE:\n{user_ctx}\n\n"
        f"CANDIDATE:\n{cand_ctx}\n\n"
        f"OVERLAP SIGNALS: {_overlap_lines(score)} "
        f"(response-likelihood score {score.score}/100)\n\n"
        "Explain how this candidate relates to the user."
    )

    try:
        msg = llm.client().messages.create(
            model=get_settings().anthropic_model,
            max_tokens=200,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        logger.info("relation synthesis unavailable (%s); saving prospect without summary", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never fail a run on the optional summary
        logger.warning("relation synthesis errored (%s); continuing", type(exc).__name__)
        return None

    if getattr(msg, "stop_reason", None) == "refusal":
        return None
    text = "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
    return text.strip() or None
