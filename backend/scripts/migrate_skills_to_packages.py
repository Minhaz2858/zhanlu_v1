#!/usr/bin/env python3
"""One-shot migration: add ``manifest.yaml`` to every bundled skill folder.

Reads each skill's ``SKILL.md`` frontmatter, distills it into a structured
``manifest.yaml``, and writes it atomically alongside the existing file.

Usage::

    python scripts/migrate_skills_to_packages.py          # execute
    python scripts/migrate_skills_to_packages.py --dry-run  # preview only

After running, the compat shim in ``skills_loader`` will prefer
``manifest.yaml`` while keeping ``SKILL.md`` as the fallback body source.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

# Make the backend package importable when run as a script
_SCRIPT_DIR = Path(__file__).resolve().parent  # backend/scripts/
_BACKEND_DIR = _SCRIPT_DIR.parent              # backend/
sys.path.insert(0, str(_BACKEND_DIR))

import re

from app.services.skills_loader import parse_frontmatter, parse_skill_file
from app.services.skills_loader.writer import write_manifest

logger = logging.getLogger("migrate_skills")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _find_skill_dirs(skills_root: Path) -> list[Path]:
    """Return all immediate child dirs of *skills_root* that contain a SKILL.md."""
    if not skills_root.is_dir():
        return []
    dirs: list[Path] = []
    for d in sorted(skills_root.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            dirs.append(d)
    return dirs


def _frontmatter_to_manifest(frontmatter: dict, category: str, body: str) -> dict:
    """Convert legacy SKILL.md frontmatter into a manifest dict.

    Preserves all known keys and normalises them to the canonical manifest
    shape (flat top-level keys matching ``manifest.schema.json``).
    """
    metadata: dict = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    hermes: dict = metadata.get("hermes", {}) if isinstance(metadata, dict) else {}
    if not isinstance(hermes, dict):
        hermes = {}

    tags: list = hermes.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    manifest: dict[str, object] = {
        "name": frontmatter.get("name", ""),
        "version": str(frontmatter.get("version", "1.0.0")),
        "description": str(frontmatter.get("description", "")),
    }

    # Optional fields — only include if present in frontmatter
    _maybe_set(manifest, frontmatter, "summary")
    _maybe_set(manifest, frontmatter, "author")
    _maybe_set(manifest, frontmatter, "license")
    _maybe_set(manifest, frontmatter, "category", default=category)
    _maybe_set(manifest, frontmatter, "trigger")
    _maybe_set(manifest, frontmatter, "runtime")
    _maybe_set(manifest, frontmatter, "user_invocable")

    platforms = frontmatter.get("platforms")
    if isinstance(platforms, list) and platforms:
        manifest["platforms"] = platforms

    prerequisites = frontmatter.get("prerequisites")
    if isinstance(prerequisites, dict) and prerequisites:
        manifest["prerequisites"] = prerequisites

    if tags:
        manifest["tags"] = tags

    # Inferred fields
    manifest["source"] = "bundled"
    manifest["requires_sandbox"] = False

    # Estimate artifact types from body content
    art_types: list[str] = []
    body_lower = body.lower()
    if "pptx" in body_lower or "presentation" in body_lower or "slide" in body_lower:
        art_types.append("pptx")
    if "docx" in body_lower or "word document" in body_lower:
        art_types.append("docx")
    if "pdf" in body_lower:
        art_types.append("pdf")
    if "xlsx" in body_lower or "spreadsheet" in body_lower or "excel" in body_lower:
        art_types.append("xlsx")
    if "html" in body_lower or "dashboard" in body_lower:
        art_types.append("html")
    if "markdown" in body_lower or ".md" in body_lower:
        art_types.append("md")
    if art_types:
        manifest["artifact_types"] = art_types

    return manifest


def _parse_frontmatter_lenient(content: str) -> tuple[dict, str]:
    """Fallback frontmatter parser that uses regex when YAML parsing fails.

    Extracts ``key: value`` pairs from the frontmatter block using a
    line-by-line regex approach.  This handles poorly-quoted descriptions
    that confuse strict YAML parsers.
    """
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_text = parts[1].strip()
    body = parts[2].strip()

    fm: dict = {}
    current_key: str | None = None
    current_value: list[str] = []

    for line in fm_text.splitlines():
        # Check if this line starts a new key: value pair (key at start, then colon)
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)", line)
        if m:
            # Save previous key
            if current_key is not None:
                fm[current_key] = " ".join(current_value).strip()
            current_key = m.group(1)
            val = m.group(2)
            current_value = [val] if val else []
        elif current_key is not None:
            # Continuation line of a multi-line value
            current_value.append(line.strip())

    if current_key is not None:
        fm[current_key] = " ".join(current_value).strip()

    return fm, body


def _safe_parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse frontmatter, falling back to lenient regex on YAML failure."""
    result = parse_frontmatter(content)
    if result[0]:
        return result
    # YAML parsing returned empty dict — try lenient fallback
    return _parse_frontmatter_lenient(content)


def _maybe_set(target: dict, source: dict, key: str, default: object = None) -> None:
    val = source.get(key)
    if val is not None and val != "":
        target[key] = str(val) if not isinstance(val, (bool, list, dict)) else val
    elif default is not None:
        target[key] = default


def migrate(skills_root: Path, *, dry_run: bool = False) -> int:
    """Run the migration, returning the number of skills migrated."""
    dirs = _find_skill_dirs(skills_root)
    migrated = 0
    skipped = 0
    errors = 0

    for skill_dir in dirs:
        name = skill_dir.name
        manifest_path = skill_dir / "manifest.yaml"

        if manifest_path.exists():
            logger.info("SKIP %s — manifest.yaml already exists", name)
            skipped += 1
            continue

        try:
            # Parse the existing SKILL.md, using lenient fallback if YAML is broken
            skill_md_path = skill_dir / "SKILL.md"
            raw = skill_md_path.read_text(encoding="utf-8")
            frontmatter, body = _safe_parse_frontmatter(raw)

            if not frontmatter:
                logger.warning("SKIP %s — no valid frontmatter in SKILL.md", name)
                skipped += 1
                continue

            manifest = _frontmatter_to_manifest(frontmatter, category=name, body=body)

            if dry_run:
                yaml_preview = yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False)
                logger.info(
                    "DRY-RUN %s → would write:\n%s",
                    name,
                    "".join(f"    {line}\n" for line in yaml_preview.splitlines()),
                )
                migrated += 1
            else:
                write_manifest(skill_dir, manifest, overwrite=False)
                logger.info("OK   %s → manifest.yaml written", name)
                migrated += 1

        except Exception as e:
            logger.error("FAIL %s — %s: %s", name, type(e).__name__, e)
            errors += 1

    total = len(dirs)
    logger.info(
        "Summary: %d total | %d migrated | %d skipped | %d errors",
        total, migrated, skipped, errors,
    )
    return migrated


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate skills to folder packages")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview manifests without writing files",
    )
    parser.add_argument(
        "--skills-dir",
        default=str(_BACKEND_DIR / "skills"),
        help="Path to the skills directory (default: backend/skills)",
    )
    args = parser.parse_args()

    skills_root = Path(args.skills_dir).resolve()
    if not skills_root.is_dir():
        logger.error("Skills directory not found: %s", skills_root)
        sys.exit(1)

    logger.info("Skills root: %s", skills_root)
    logger.info("Mode: %s", "DRY-RUN" if args.dry_run else "LIVE")

    count = migrate(skills_root, dry_run=args.dry_run)
    if args.dry_run:
        logger.info("Dry-run complete — %d manifest(s) would be written. Run without --dry-run to apply.", count)
    else:
        logger.info("Migration complete — %d manifest(s) written.", count)


if __name__ == "__main__":
    main()
