"""Marketplace service — publish, browse, install, and rate community skills.

Separate from the ``tools`` table. Published skills live in the
``marketplace_skills`` table; installing copies them into the user's
skills filesystem + ``tools`` table.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.models.marketplace_skill import MarketplaceSkill
from app.models.marketplace_rating import MarketplaceRating
from app.models.tool import Tool
from app.services.skill_sync import write_skill_md, USER_SKILLS_DIR
from app.services.skills_loader import SkillsRegistry

logger = logging.getLogger(__name__)

MAX_SKILL_MD_BYTES = 100 * 1024  # 100 KB
MARKETPLACE_SECRET = "zhanlu-marketplace-v1"  # signing secret (per-publisher in prod)


# ─── Content validation ────────────────────────────────────────────────

def validate_skill_md(skill_md: str) -> list[str]:
    """Validate a SKILL.md body. Returns a list of error messages (empty = valid)."""
    errors: list[str] = []

    if not skill_md or not skill_md.strip():
        errors.append("skill_md is empty")
        return errors

    if len(skill_md.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        errors.append(f"skill_md exceeds {MAX_SKILL_MD_BYTES // 1024}KB limit")

    # Parse YAML frontmatter
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---", skill_md, re.DOTALL)
    if frontmatter_match:
        try:
            fm = yaml.safe_load(frontmatter_match.group(1))
        except yaml.YAMLError as exc:
            errors.append(f"Invalid YAML frontmatter: {exc}")
        else:
            if isinstance(fm, dict):
                if not fm.get("name"):
                    errors.append("YAML frontmatter missing 'name' field")
                if not fm.get("description"):
                    errors.append("YAML frontmatter missing 'description' field")
    else:
        errors.append("Missing YAML frontmatter (must start with ---)")

    return errors


def sign_skill_content(skill_md: str, publisher_id: str) -> str:
    """Create an HMAC-SHA256 signature for skill content verification."""
    key = f"{MARKETPLACE_SECRET}:{publisher_id}".encode("utf-8")
    return hmac.new(key, skill_md.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_skill_signature(skill_md: str, publisher_id: str, signature: str) -> bool:
    """Verify a skill's signature matches its content."""
    expected = sign_skill_content(skill_md, publisher_id)
    return hmac.compare_digest(expected, signature)


# ─── Publish ────────────────────────────────────────────────────────────

def publish_skill(
    db: Session,
    *,
    name: str,
    description: str,
    skill_md: str,
    category: str | None = None,
    version: str = "1.0.0",
    publisher_id: str | None = None,
    publisher_name: str | None = None,
    github_url: str | None = None,
    tags: list[str] | None = None,
    author_email: str | None = None,
) -> MarketplaceSkill:
    """Publish a skill to the marketplace."""
    # Validate
    errors = validate_skill_md(skill_md)
    if errors:
        raise ValueError("; ".join(errors))

    # Check for duplicate name (active, non-deleted)
    existing = (
        db.query(MarketplaceSkill)
        .filter(
            MarketplaceSkill.name == name,
            MarketplaceSkill.is_deleted == False,
        )
        .first()
    )
    if existing:
        raise ValueError(f"Skill '{name}' already exists in marketplace")

    # Extract summary from frontmatter or description
    summary = _extract_summary(skill_md) or (description[:500] if description else "")

    # Sign content
    signature = sign_skill_content(skill_md, publisher_id or "anonymous")

    skill = MarketplaceSkill(
        name=name,
        description=description,
        summary=summary,
        category=category,
        version=version,
        publisher_id=publisher_id,
        publisher_name=publisher_name,
        skill_md=skill_md,
        github_url=github_url,
        tags=tags,
        author_email=author_email,
        signature=signature,
        is_verified=False,  # manual verification in admin
        is_published=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def _extract_summary(skill_md: str) -> str | None:
    """Extract summary from frontmatter or first non-heading paragraph."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---", skill_md, re.DOTALL)
    if fm_match:
        try:
            fm = yaml.safe_load(fm_match.group(1))
            if isinstance(fm, dict) and fm.get("summary"):
                return str(fm["summary"])[:500]
        except yaml.YAMLError:
            pass

    # Fallback: first non-empty paragraph after frontmatter
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", skill_md, flags=re.DOTALL)
    lines = body.strip().split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:500]
    return None


# ─── Browse ─────────────────────────────────────────────────────────────

def browse_skills(
    db: Session,
    *,
    category: str | None = None,
    query: str | None = None,
    sort: str = "popular",
    page: int = 1,
    page_size: int = 20,
    verified_only: bool = False,
) -> dict[str, Any]:
    """Browse marketplace skills with optional filtering and pagination."""
    q = db.query(MarketplaceSkill).filter(
        MarketplaceSkill.is_deleted == False,
        MarketplaceSkill.is_published == True,
    )

    if verified_only:
        q = q.filter(MarketplaceSkill.is_verified == True)
    if category:
        q = q.filter(MarketplaceSkill.category == category)
    if query:
        search = f"%{query}%"
        q = q.filter(
            (MarketplaceSkill.name.ilike(search))
            | (MarketplaceSkill.description.ilike(search))
            | (MarketplaceSkill.summary.ilike(search))
        )

    total = q.count()

    if sort == "popular":
        q = q.order_by(MarketplaceSkill.download_count.desc())
    elif sort == "rating":
        q = q.order_by(MarketplaceSkill.avg_rating.desc().nullslast())
    elif sort == "newest":
        q = q.order_by(MarketplaceSkill.created_date.desc())
    elif sort == "name":
        q = q.order_by(MarketplaceSkill.name.asc())
    else:
        q = q.order_by(MarketplaceSkill.download_count.desc())

    offset = (page - 1) * page_size
    skills = q.offset(offset).limit(page_size).all()

    return {
        "skills": [s.to_dict() for s in skills],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


def get_skill_detail(db: Session, skill_id: str) -> MarketplaceSkill | None:
    """Get a single marketplace skill by ID."""
    return (
        db.query(MarketplaceSkill)
        .filter(
            MarketplaceSkill.id == skill_id,
            MarketplaceSkill.is_deleted == False,
        )
        .first()
    )


# ─── Install ────────────────────────────────────────────────────────────

def install_skill(
    db: Session,
    marketplace_skill_id: str,
    *,
    user_id: str | None = None,
) -> Tool:
    """Install a marketplace skill into the user's tools.

    Copies skill_md from MarketplaceSkill → writes to filesystem
    (~/.zhanlu/skills/<cat>/<name>/SKILL.md) and creates a Tool row.
    Increments the marketplace download counter.
    """
    mskill = (
        db.query(MarketplaceSkill)
        .filter(
            MarketplaceSkill.id == marketplace_skill_id,
            MarketplaceSkill.is_deleted == False,
        )
        .first()
    )
    if not mskill:
        raise ValueError("Marketplace skill not found")
    if not mskill.skill_md:
        raise ValueError("Marketplace skill has no content")

    category = (mskill.category or "general").lower()
    tool_name = mskill.name

    # Check if already installed by this user
    existing = (
        db.query(Tool)
        .filter(
            Tool.name == tool_name,
            Tool.created_by_id == user_id,
            Tool.is_deleted == False,
        )
        .first()
    )
    if existing:
        # Already installed — return existing row
        return existing

    # Write to filesystem
    try:
        write_skill_md(
            name=tool_name,
            description=mskill.description or "",
            body=mskill.skill_md,
            version=mskill.version,
            tags=mskill.tags or [],
            category=category,
            source="marketplace_user",
        )
    except Exception as exc:
        logger.warning("Failed to write marketplace skill to filesystem: %s", exc)

    # Create Tool row
    tool = Tool(
        name=tool_name,
        description=mskill.description,
        kind="custom_tool",
        category=category,
        source="marketplace_user",
        version=mskill.version,
        publisher=mskill.publisher_name,
        skill_md=mskill.skill_md,
        summary=mskill.summary,
        tags_progressive=mskill.tags,
        created_by_id=user_id,
    )
    db.add(tool)

    # Increment download count on marketplace row
    mskill.download_count = (mskill.download_count or 0) + 1
    db.add(mskill)

    db.commit()
    db.refresh(tool)

    # Reload skills registry so the new skill is available immediately
    try:
        from app.services.skills_loader import get_skills_registry
        get_skills_registry().load()
    except Exception as exc:
        logger.warning("Failed to reload skills registry after install: %s", exc)

    return tool


# ─── Rate ───────────────────────────────────────────────────────────────

def rate_skill(
    db: Session,
    marketplace_skill_id: str,
    *,
    user_id: str | None,
    rating: int,
    review: str | None = None,
) -> MarketplaceRating:
    """Rate a marketplace skill (1-5) and update aggregate metrics."""
    if rating < 1 or rating > 5:
        raise ValueError("Rating must be between 1 and 5")

    mskill = (
        db.query(MarketplaceSkill)
        .filter(
            MarketplaceSkill.id == marketplace_skill_id,
            MarketplaceSkill.is_deleted == False,
        )
        .first()
    )
    if not mskill:
        raise ValueError("Marketplace skill not found")

    # Upsert — one rating per user per skill
    existing = (
        db.query(MarketplaceRating)
        .filter(
            MarketplaceRating.marketplace_skill_id == marketplace_skill_id,
            MarketplaceRating.user_id == user_id,
            MarketplaceRating.is_deleted == False,
        )
        .first()
    )
    if existing:
        existing.rating = rating
        existing.review = review
        db.add(existing)
        db.flush()
    else:
        mr = MarketplaceRating(
            marketplace_skill_id=marketplace_skill_id,
            user_id=user_id,
            rating=rating,
            review=review,
        )
        db.add(mr)
        db.flush()

    # Recompute aggregate
    all_ratings = (
        db.query(MarketplaceRating)
        .filter(
            MarketplaceRating.marketplace_skill_id == marketplace_skill_id,
            MarketplaceRating.is_deleted == False,
        )
        .all()
    )
    total = sum(r.rating for r in all_ratings)
    count = len(all_ratings)
    mskill.avg_rating = float(total) / float(count) if count > 0 else None
    mskill.ratings_count = count
    db.add(mskill)

    db.commit()
    db.refresh(existing or mr)
    return existing or mr


# ─── Helper: get ratings for display ────────────────────────────────────

def get_skill_ratings(
    db: Session,
    marketplace_skill_id: str,
    page: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    """Get paginated ratings for a marketplace skill."""
    total = (
        db.query(MarketplaceRating)
        .filter(
            MarketplaceRating.marketplace_skill_id == marketplace_skill_id,
            MarketplaceRating.is_deleted == False,
        )
        .count()
    )
    ratings = (
        db.query(MarketplaceRating)
        .filter(
            MarketplaceRating.marketplace_skill_id == marketplace_skill_id,
            MarketplaceRating.is_deleted == False,
        )
        .order_by(MarketplaceRating.created_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "ratings": [r.to_dict() for r in ratings],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
