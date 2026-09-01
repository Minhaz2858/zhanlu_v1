"""Skill curator -- detect overlapping skills and suggest consolidation.

Skills in Zhanlu are stored as ``Tool`` records with ``tool_type="skill"``.
Over time, the skill library can accumulate overlapping or stale skills.
This module provides:

1. **Overlap detection**: find skills with similar content (by token overlap
   or embedding similarity when available).
2. **Stale detection**: find skills that haven't been used in N days.
3. **Consolidation suggestions**: recommend merges or archival.

Designed to run as a background task or be triggered manually via an API
endpoint. Does NOT auto-delete skills -- only suggests actions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Token overlap threshold above which two skills are considered overlapping.
OVERLAP_THRESHOLD = 0.6

# Skills not used in this many days are considered stale.
STALE_DAYS = 60

# Minimum content length to consider for overlap (skip tiny skills).
MIN_CONTENT_LENGTH = 50


@dataclass
class SkillInfo:
    """Lightweight skill metadata for comparison."""
    id: str
    name: str
    description: str
    content: str
    category: str = ""
    last_used: datetime | None = None
    usage_count: int = 0
    source: str = ""


@dataclass
class OverlapPair:
    """Two skills that overlap significantly."""
    skill_a: SkillInfo
    skill_b: SkillInfo
    overlap_score: float
    shared_tokens: list[str] = field(default_factory=list)


@dataclass
class CurationReport:
    """Summary of a skill curation run."""
    total_skills: int = 0
    overlapping_pairs: int = 0
    stale_skills: int = 0
    merge_suggestions: list[dict] = field(default_factory=list)
    archive_suggestions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_skills": self.total_skills,
            "overlapping_pairs": self.overlapping_pairs,
            "stale_skills": self.stale_skills,
            "merge_suggestions": self.merge_suggestions,
            "archive_suggestions": self.archive_suggestions,
        }


def _tokenize(text: str) -> set[str]:
    """Tokenize text for overlap comparison."""
    words = re.findall(r"\w+", text.lower())
    return {w for w in words if len(w) > 2}  # skip very short tokens


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def _load_skills(db: Session) -> list[SkillInfo]:
    """Load all skills from the database.

    Defensive about model schema — uses getattr for attributes that may
    not exist on all Tool model versions.
    """
    try:
        from app.models.tool import Tool
        tools = db.query(Tool).all()
    except Exception:
        logger.debug("Tool model not available or query failed")
        return []

    skills: list[SkillInfo] = []
    for t in tools:
        # Be defensive about attribute names across model versions
        tool_type = (
            getattr(t, "tool_type", None)
            or getattr(t, "kind", None)
            or getattr(t, "type", None)
        )
        is_deleted = getattr(t, "is_deleted", False)
        if is_deleted:
            continue
        # If tool_type is set and not "skill", skip
        if tool_type and tool_type != "skill":
            continue

        content = getattr(t, "content", "") or getattr(t, "description", "") or ""
        if len(content) < MIN_CONTENT_LENGTH:
            continue
        skills.append(SkillInfo(
            id=str(t.id),
            name=getattr(t, "name", ""),
            description=getattr(t, "description", ""),
            content=content,
            category=getattr(t, "category", ""),
            last_used=getattr(t, "last_used_at", None) or getattr(t, "updated_date", None),
            usage_count=getattr(t, "usage_count", 0) or 0,
            source=getattr(t, "source", ""),
        ))

    return skills


def find_overlapping_skills(
    db: Session,
    threshold: float = OVERLAP_THRESHOLD,
) -> list[OverlapPair]:
    """Find pairs of skills with significant content overlap.

    Uses Jaccard similarity on tokenized content. Returns pairs sorted
    by overlap score descending.
    """
    skills = _load_skills(db)
    if len(skills) < 2:
        return []

    # Pre-compute token sets
    token_sets = {s.id: _tokenize(s.content + " " + s.description) for s in skills}

    pairs: list[OverlapPair] = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            score = _jaccard_similarity(token_sets[a.id], token_sets[b.id])
            if score >= threshold:
                shared = sorted(token_sets[a.id] & token_sets[b.id])[:10]
                pairs.append(OverlapPair(
                    skill_a=a, skill_b=b,
                    overlap_score=score, shared_tokens=shared,
                ))

    pairs.sort(key=lambda p: -p.overlap_score)
    return pairs


def find_stale_skills(
    db: Session,
    stale_days: int = STALE_DAYS,
) -> list[SkillInfo]:
    """Find skills that haven't been used in ``stale_days`` days."""
    skills = _load_skills(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    stale: list[SkillInfo] = []
    for s in skills:
        if s.usage_count == 0:
            stale.append(s)
            continue
        if s.last_used:
            last = s.last_used
            if isinstance(last, str):
                try:
                    last = datetime.fromisoformat(last)
                except ValueError:
                    continue
            if last < cutoff:
                stale.append(s)

    return stale


def run_skill_curation(
    db: Session,
    *,
    overlap_threshold: float = OVERLAP_THRESHOLD,
    stale_days: int = STALE_DAYS,
) -> CurationReport:
    """Run the full skill curation pipeline.

    Returns a CurationReport with suggestions (does NOT modify skills).
    """
    report = CurationReport()
    skills = _load_skills(db)
    report.total_skills = len(skills)

    # Find overlapping pairs
    pairs = find_overlapping_skills(db, overlap_threshold)
    report.overlapping_pairs = len(pairs)

    for pair in pairs:
        report.merge_suggestions.append({
            "skill_a": {"id": pair.skill_a.id, "name": pair.skill_a.name},
            "skill_b": {"id": pair.skill_b.id, "name": pair.skill_b.name},
            "overlap_score": round(pair.overlap_score, 3),
            "shared_tokens": pair.shared_tokens,
            "suggestion": f"Consider merging '{pair.skill_a.name}' and '{pair.skill_b.name}' (overlap: {pair.overlap_score:.1%})",
        })

    # Find stale skills
    stale = find_stale_skills(db, stale_days)
    report.stale_skills = len(stale)

    for s in stale:
        report.archive_suggestions.append({
            "id": s.id,
            "name": s.name,
            "usage_count": s.usage_count,
            "last_used": str(s.last_used) if s.last_used else "never",
            "suggestion": f"Consider archiving '{s.name}' (unused, stale)",
        })

    logger.info(
        "Skill curation: %d skills, %d overlapping pairs, %d stale",
        report.total_skills, report.overlapping_pairs, report.stale_skills,
    )

    return report


__all__ = [
    "CurationReport",
    "OverlapPair",
    "SkillInfo",
    "find_overlapping_skills",
    "find_stale_skills",
    "run_skill_curation",
    "OVERLAP_THRESHOLD",
    "STALE_DAYS",
]
