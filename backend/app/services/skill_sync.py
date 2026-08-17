"""Skill synchronisation layer — bridges DB ``tools`` table and filesystem .md files.

Provides:
- ``write_skill_md()`` — persist a skill as ``~/.zhanlu/skills/<category>/<name>/SKILL.md``
- ``delete_skill_md()`` — remove a skill's .md file
- ``sync_marketplace_to_db()`` — one-time sync of bundled .md skills into the DB
- ``reload_skills_registry()`` — trigger in-memory registry reload

This module is the single source of truth for filesystem writes.  The
SkillsRegistry (``skills_loader``) remains the read side for filesystem
content; the DB ``tools`` table is the read side for catalog/metadata.
``get_skill_prompt_for_agent`` in ``skills_loader`` queries both.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# User skills live under ~/.zhanlu/skills/ — same directory the SkillsRegistry scans.
USER_SKILLS_DIR = Path.home() / ".zhanlu" / "skills"


def _sanitize(name: str) -> str:
    """Make a string safe for use as a filesystem directory/file name.

    Strips path separators, control chars, and common shell metacharacters.
    Collapses whitespace to hyphens.
    """
    # Replace spaces with hyphens
    cleaned = re.sub(r"\s+", "-", name.strip())
    # Remove anything that isn't alphanumeric, hyphen, underscore, or dot
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", cleaned)
    # Prevent path traversal / hidden files
    cleaned = cleaned.lstrip(".")
    # Collapse multiple hyphens
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned or "unnamed-skill"


def write_skill_md(
    name: str,
    description: str,
    body: str,
    category: str = "custom",
    trigger: str = "",
    version: str = "1.0.0",
    author: str = "user",
    tags: list[str] | None = None,
    summary: str | None = None,
    runtime: str | None = None,
    references: dict[str, str] | None = None,
    assets: dict[str, bytes] | None = None,
    *,
    overwrite: bool = True,
) -> str:
    """Write a SKILL.md file to ``~/.zhanlu/skills/<category>/<name>/SKILL.md``.

    The YAML frontmatter follows the same schema used by bundled marketplace
    skills so the existing ``parse_skill_file`` loader picks them up.

    Supports the Kimi-style folder anatomy: optional ``references``
    (``{filename.md: markdown_content}``) are written under ``references/`` and
    optional ``assets`` (``{relative_path: bytes}``) are written under
    ``assets/`` — e.g. ``{"templates/report.docx": <bytes>}``.

    Returns the absolute path of the written SKILL.md file.
    """
    safe_category = _sanitize(category) if category else "custom"
    safe_name = _sanitize(name)
    skill_dir = USER_SKILLS_DIR / safe_category / safe_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / "SKILL.md"

    frontmatter: dict[str, Any] = {
        "name": name,  # keep original display name in frontmatter
        "description": description,
        "version": version,
        "author": author,
        "category": category,
    }
    if trigger:
        frontmatter["trigger"] = trigger
    if summary:
        frontmatter["summary"] = summary
    if tags:
        frontmatter["metadata"] = {"hermes": {"tags": tags}}
    if runtime:
        frontmatter["runtime"] = runtime

    # Record the folder package inventory in the frontmatter so the loader can
    # surface references/assets without a separate manifest.yaml file.
    if references:
        frontmatter["references_manifest"] = {
            fn: "" for fn in references.keys()
        }
    if assets:
        frontmatter["assets_manifest"] = {
            rel: "" for rel in assets.keys()
        }

    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + (body or "").strip()
        + "\n"
    )

    md_path.write_text(content, encoding="utf-8")
    logger.info("Wrote skill .md to %s", md_path)

    # Write references/ and assets/ via the atomic package writer.
    from app.services.skills_loader import writer as _writer

    if references:
        for filename, ref_content in references.items():
            _writer.write_reference(skill_dir, filename, ref_content, overwrite=overwrite)
    if assets:
        for rel_path, asset_bytes in assets.items():
            _writer.write_asset(skill_dir, rel_path, asset_bytes, overwrite=overwrite)

    # Trigger dry-run validation gate (non-blocking — failure is a warning, not an error)
    try:
        from app.services.skill_dry_run import trigger_dry_run_after_save
        trigger_dry_run_after_save(name)
    except Exception as exc:
        logger.debug("Dry-run gate trigger failed (non-fatal): %s", exc)

    return str(md_path)


def delete_skill_md(name: str, category: str = "custom") -> bool:
    """Remove the .md file (and its parent dir if empty) for a skill.

    Returns True if something was deleted.
    """
    safe_category = _sanitize(category) if category else "custom"
    safe_name = _sanitize(name)
    skill_dir = USER_SKILLS_DIR / safe_category / safe_name
    md_path = skill_dir / "SKILL.md"

    deleted = False
    if md_path.exists():
        md_path.unlink()
        deleted = True
    # Clean up empty parent dir
    if skill_dir.exists() and not any(skill_dir.iterdir()):
        skill_dir.rmdir()
    if deleted:
        logger.info("Deleted skill .md at %s", md_path)
        try:
            reload_skills_registry()
        except Exception:
            pass
    return deleted


def reload_skills_registry() -> None:
    """Trigger SkillsRegistry.reload() so newly written skills are immediately available."""
    try:
        from app.services.skills_loader import get_skills_registry
        get_skills_registry().reload()
    except Exception as e:
        logger.warning("SkillsRegistry reload failed (non-fatal): %s", e)


def sync_marketplace_to_db(db) -> dict[str, int]:
    """Sync filesystem-bundled skills into the DB ``tools`` table.

    This is an idempotent UPSERT keyed on ``(name, source='marketplace')``:

    - If no DB row exists for a filesystem skill, insert one with
      ``source='marketplace'`` so the frontend catalog can list it.
    - If a DB row already exists for the same name, update its content
      fields (description, trigger, category, skill_md, version,
      publisher) so a fresh edit to the .md file propagates to the DB
      on the next server start. We do NOT touch ``call_count`` or
      ``created_by_id`` — those are runtime state, not derived from
      the file.
    - DB rows with ``source != 'marketplace'`` (builtin, custom, user)
      are NEVER updated by this function, even if a marketplace skill
      happens to share the name. The marketplace/builtin namespace
      boundary is preserved.

    Returns a stats dict ``{"inserted": N, "updated": M}`` so the
    caller can log meaningful progress. The return type was changed
    from ``int`` (inserted count) to ``dict`` — callers that only need
    the inserted count should use ``stats["inserted"]``.
    """
    from app.models.tool import Tool
    from app.services.skills_loader import get_skills_registry

    registry = get_skills_registry()
    skills = registry.list_skills()

    # Build a name -> existing DB row map for marketplace-sourced rows.
    # Only marketplace rows are subject to upsert; other sources (builtin,
    # custom) are user-managed and must not be touched.
    existing = {
        row.name: row
        for row in db.query(Tool).filter(
            Tool.source == "marketplace",
            Tool.is_deleted == False,
        ).all()
    }

    from app.services.synexia.default_skills import DEFAULT_SKILL_NAMES

    inserted = 0
    updated = 0
    seen: set[str] = set()
    for skill in skills:
        seen.add(skill.name)
        # Determine if this is one of the 6 built-in default skills
        _is_default = skill.name in DEFAULT_SKILL_NAMES
        row = existing.get(skill.name)
        if row is None:
            tool = Tool(
                name=skill.name,
                description=skill.description,
                kind="system_skill",
                trigger=skill.trigger or "",
                category=skill.category or "general",
                source="marketplace",
                # Persist the author from the SKILL.md frontmatter as-is.
                # If the file has no ``author:`` field, store NULL (empty
                # string in the DB) so the frontend can render an empty
                # publisher badge instead of stamping every marketplace
                # skill with a hardcoded "hermes" label. The previous
                # ``skill.author or "hermes"`` fallback produced a uniform
                # "hermes" tag on every card — visual noise that conveyed
                # no information. The frontend hides the tag for the
                # sentinel "hermes" value AND for empty values, so the DB
                # default can safely be empty.
                publisher=skill.author or None,
                version=skill.version,
                skill_md=skill.body or "",
                summary=skill.summary or skill.description[:200],
                tags_progressive=skill.tags if skill.tags else None,
                enabled=True,
                status="active",
                is_default=_is_default,
            )
            db.add(tool)
            inserted += 1
        else:
            # Update only content fields derived from the .md file. Skip
            # update entirely if nothing changed (avoids needless writes
            # and ``updated_date`` churn).
            new_desc = skill.description
            new_trigger = skill.trigger or ""
            new_cat = skill.category or "general"
            new_md = skill.body or ""
            new_pub = skill.author or None
            new_summary = skill.summary or ""
            # Build tags_progressive from skill tags (frontmatter metadata.hermes.tags)
            new_tags: list[str] | None = skill.tags if skill.tags else None
            # Also check if is_default needs updating (skill was promoted/demoted)
            _is_default_changed = (row.is_default or False) != _is_default
            if (row.description != new_desc
                or (row.trigger or "") != new_trigger
                or (row.category or "") != new_cat
                or (row.skill_md or "") != new_md
                or (row.publisher or "") != (new_pub or "")
                or (row.summary or "") != (new_summary or "")
                or _is_default_changed):
                row.description = new_desc
                row.trigger = new_trigger
                row.category = new_cat
                row.skill_md = new_md
                row.version = skill.version
                # Persist author verbatim (NULL if missing). Avoid the
                # legacy "hermes" sentinel so the frontend renders an
                # empty publisher badge instead of a constant "hermes"
                # tag on every marketplace card.
                row.publisher = new_pub
                row.enabled = True
                row.status = row.status or "active"
                row.summary = new_summary
                row.tags_progressive = new_tags
                row.is_default = _is_default
                updated += 1

    # Skills that exist in DB with source='marketplace' but are no longer
    # on disk (e.g. someone deleted a category) get soft-deleted so they
    # don't keep showing up in the catalog as ghosts. We do this only
    # for marketplace-sourced rows, never for user-owned skills.
    for name, row in existing.items():
        if name in seen:
            continue
        row.is_deleted = True
        logger.info("Soft-deleting orphan marketplace skill %r (file removed)", name)

    if inserted or updated:
        db.commit()
        logger.info(
            "Marketplace sync: inserted=%d updated=%d, total on disk=%d",
            inserted, updated, len(skills),
        )
    else:
        logger.debug("Marketplace skills already in sync — nothing to do")

    return {"inserted": inserted, "updated": updated}
