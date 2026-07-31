"""Skills dynamic loading system — load skills from .md files on demand.

Scans the backend/skills/ directory for .md files with YAML frontmatter,
parses skill metadata, and makes them available for:
- Slash command invocation (e.g. /apple-notes)
- On-demand injection into agent system prompts
- ToolRegistry registration for user-invocable skills

As of Phase A, also supports folder-based skill packages with
manifest.yaml + JSON Schema.  The compat shim loads manifest.yaml first,
falling back to SKILL.md frontmatter for legacy skills.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jsonschema import validate as _validate_schema, ValidationError as _ValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load the manifest JSON Schema once at module level
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).resolve().parent / "manifest.schema.json"
_MANIFEST_SCHEMA: dict[str, Any] | None = None


def _load_manifest_schema() -> dict[str, Any]:
    global _MANIFEST_SCHEMA
    if _MANIFEST_SCHEMA is None:
        try:
            _MANIFEST_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load manifest JSON Schema: %s", e)
            _MANIFEST_SCHEMA = {}
    return _MANIFEST_SCHEMA


@dataclass
class SkillMetadata:
    """Metadata for a skill loaded from a .md file."""

    name: str
    description: str
    file_path: str
    body: str = ""
    version: str = "1.0.0"
    author: str = ""
    license: str = ""
    platforms: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""
    trigger: str = ""
    summary: str = ""
    runtime: str = ""
    user_invocable: bool = True
    prerequisites: dict[str, Any] = field(default_factory=dict)
    source: str = "bundled"
    scan_findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "file_path": self.file_path,
            "version": self.version,
            "author": self.author,
            "license": self.license,
            "platforms": self.platforms,
            "tags": self.tags,
            "category": self.category,
            "trigger": self.trigger,
            "summary": self.summary,
            "runtime": self.runtime,
            "user_invocable": self.user_invocable,
            "source": self.source,
        }


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    frontmatter_text = parts[1].strip()
    body = parts[2].strip()
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return {}, content
    except yaml.YAMLError as e:
        logger.warning("YAML parse error: %s", e)
        return {}, content
    return frontmatter, body


# ── Manifest loading helpers ───────────────────────────────────────────


def parse_manifest(filepath: Path) -> dict[str, Any] | None:
    """Parse a manifest.yaml file from a skill package folder.

    Args:
        filepath: Path to ``manifest.yaml``.

    Returns:
        A dict of manifest data, or ``None`` if the file cannot be parsed.
    """
    try:
        raw = filepath.read_text(encoding="utf-8")
        manifest = yaml.safe_load(raw)
        if not isinstance(manifest, dict):
            logger.warning("manifest.yaml is not a dict: %s", filepath)
            return None
        return manifest
    except yaml.YAMLError as e:
        logger.warning("YAML parse error in manifest %s: %s", filepath, e)
        return None
    except Exception as e:
        logger.warning("Failed to read manifest %s: %s", filepath, e)
        return None


def validate_manifest(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a manifest dict against the JSON Schema.

    Args:
        manifest: The parsed manifest dict.

    Returns:
        A ``(is_valid, errors)`` tuple.  ``errors`` is a list of
        human-readable validation messages (empty when valid).
    """
    schema = _load_manifest_schema()
    if not schema:
        return True, []  # No schema → skip validation gracefully
    try:
        _validate_schema(instance=manifest, schema=schema)
        return True, []
    except _ValidationError as e:
        return False, [str(e)]


def load_skill_package(package_dir: Path, source: str = "bundled") -> SkillMetadata | None:
    """Load a skill from a folder package with ``manifest.yaml``.

    Resolution order within the package:
    1. ``manifest.yaml`` for metadata
    2. ``SKILL.md`` for the methodology body (fallback)
    3. ``schemas/input.schema.json`` / ``schemas/output.schema.json`` for schemas

    Args:
        package_dir: The skill package directory (contains manifest.yaml).
        source: Origin tag ("bundled", "user", "marketplace", "generated").

    Returns:
        A ``SkillMetadata`` instance, or ``None`` if no valid manifest found.
    """
    manifest_path = package_dir / "manifest.yaml"
    if not manifest_path.exists():
        return None

    manifest = parse_manifest(manifest_path)
    if manifest is None:
        return None

    is_valid, errors = validate_manifest(manifest)
    if not is_valid:
        logger.warning(
            "Manifest validation failed for %s: %s",
            manifest_path, "; ".join(errors),
        )
        return None

    # Body: prefer SKILL.md in same directory, fallback to manifest description
    skill_md_path = package_dir / "SKILL.md"
    body = ""
    if skill_md_path.exists():
        try:
            raw = skill_md_path.read_text(encoding="utf-8")
            # Use lenient fallback; some frontmatter files have unquoted colons
            _, body = _safe_parse_frontmatter(raw)
        except Exception as e:
            logger.warning("Failed to read SKILL.md in %s: %s", package_dir, e)

    # Load schemas if present
    input_schema = _load_json_schema(package_dir / "schemas" / "input.schema.json")
    output_schema = _load_json_schema(package_dir / "schemas" / "output.schema.json")

    name = manifest.get("name", package_dir.name)
    tags = manifest.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    return SkillMetadata(
        name=name,
        description=manifest.get("description", ""),
        file_path=str(package_dir),
        body=body,
        version=manifest.get("version", "1.0.0"),
        author=manifest.get("author", ""),
        license=manifest.get("license", ""),
        platforms=manifest.get("platforms", []) or [],
        tags=tags,
        category=manifest.get("category", package_dir.parent.name if package_dir.parent != Path(".") else ""),
        trigger=manifest.get("trigger", ""),
        summary=manifest.get("summary", ""),
        runtime=manifest.get("runtime", ""),
        user_invocable=manifest.get("user_invocable", True),
        prerequisites=manifest.get("prerequisites", {}) or {},
        source=manifest.get("source", source),
    )


def load_legacy_skill_md(filepath: Path, source: str = "bundled") -> SkillMetadata | None:
    """Load a skill from a legacy ``SKILL.md`` file with YAML frontmatter.

    This is the *old* path — used as fallback when no ``manifest.yaml``
    exists.  Functionally identical to the original ``parse_skill_file``.

    Args:
        filepath: Path to ``SKILL.md``.
        source: Origin tag.

    Returns:
        A ``SkillMetadata`` instance, or ``None`` on failure.
    """
    return parse_skill_file(filepath, source=source)


def _safe_parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse frontmatter, falling back to lenient regex on YAML failure.

    Some SKILL.md files have unquoted colons in description strings
    (e.g. "Critical: this tool...") that break strict YAML parsers.
    """
    import re as _re

    # Try strict YAML parsing first, but catch its own warnings
    current_level = logger.level
    try:
        logger.setLevel(logging.ERROR)
        result = parse_frontmatter(content)
    finally:
        logger.setLevel(current_level)

    if result[0]:
        return result
    # YAML parsing returned empty dict — try lenient fallback
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_text = parts[1].strip()
    body = parts[2].strip()

    fm: dict[str, Any] = {}
    current_key: str | None = None
    current_value: list[str] = []

    for line in fm_text.splitlines():
        m = _re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)", line)
        if m:
            if current_key is not None:
                fm[current_key] = " ".join(current_value).strip()
            current_key = m.group(1)
            val = m.group(2)
            current_value = [val] if val else []
        elif current_key is not None:
            current_value.append(line.strip())

    if current_key is not None:
        fm[current_key] = " ".join(current_value).strip()

    return fm, body


def _load_json_schema(path: Path) -> dict[str, Any] | None:
    """Load a JSON Schema file if it exists, otherwise return None."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load JSON Schema %s: %s", path, e)
        return None


def parse_skill_file(filepath: Path, source: str = "bundled") -> SkillMetadata | None:
    """Parse a single skill .md file into SkillMetadata."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read skill file %s: %s", filepath, e)
        return None

    frontmatter, body = _safe_parse_frontmatter(content)
    name = frontmatter.get("name", filepath.stem)
    description = frontmatter.get("description", "")

    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    hermes_meta = metadata.get("hermes", {}) if isinstance(metadata, dict) else {}
    if not isinstance(hermes_meta, dict):
        hermes_meta = {}

    tags = hermes_meta.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    category = frontmatter.get("category") or filepath.parent.name
    trigger = frontmatter.get("trigger", "")
    summary = str(frontmatter.get("summary", ""))
    runtime = str(frontmatter.get("runtime", ""))

    meta = SkillMetadata(
        name=name,
        description=str(description),
        file_path=str(filepath),
        body=body,
        version=str(frontmatter.get("version", "1.0.0")),
        author=str(frontmatter.get("author", "")),
        license=str(frontmatter.get("license", "")),
        platforms=frontmatter.get("platforms", []) or [],
        tags=tags,
        category=category,
        trigger=str(trigger),
        summary=summary,
        runtime=runtime,
        user_invocable=bool(frontmatter.get("user_invocable", True)),
        prerequisites=frontmatter.get("prerequisites", {}) or {},
        source=source,
    )

    # B3: deterministic security scan (warn-only)
    try:
        from app.services.skill_scanner import scan_skill
        scan_result = scan_skill(meta)
        meta.scan_findings = [
            {
                "rule_id": f.rule_id,
                "severity": f.severity,
                "description": f.description,
                "line_number": f.line_number,
            }
            for f in scan_result.findings
        ]
    except Exception as exc:
        logger.debug("Skill scan failed for %s: %s (non-blocking)", name, exc)

    return meta


class SkillsRegistry:
    """Registry for all loaded skills."""

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, SkillMetadata] = {}
        self._loaded = False

    def load(self) -> dict[str, SkillMetadata]:
        """Load all skills from all sources."""
        if self._loaded:
            return self._skills
        if self.skills_dir.exists():
            self._load_from_dir(self.skills_dir, source="bundled")
        user_dir = Path.home() / ".zhanlu" / "skills"
        if user_dir.exists():
            self._load_from_dir(user_dir, source="user")
        self._loaded = True
        logger.info("Loaded %d skills from %s", len(self._skills), self.skills_dir)
        return self._skills

    def _load_from_dir(self, directory: Path, source: str = "bundled") -> None:
        """Load skills from a directory tree.

        **Compat shim** (zero-downtime): for each skill folder, checks
        ``manifest.yaml`` first.  If found, loads the full skill package.
        Otherwise falls back to ``SKILL.md`` frontmatter parsing.

        This guarantees existing single-tenant data continues to work
        without modification while allowing gradual migration to the
        new manifest format.
        """
        if not directory.exists():
            return

        seen_dirs: set[Path] = set()

        # Pass 1: manifest.yaml → skill package (preferred)
        for manifest_path in sorted(directory.rglob("manifest.yaml")):
            pkg_dir = manifest_path.parent
            # Skip if we already loaded this dir via SKILL.md scan
            if pkg_dir in seen_dirs:
                continue
            seen_dirs.add(pkg_dir)
            skill = load_skill_package(pkg_dir, source=source)
            if skill and skill.name:
                self._skills[skill.name] = skill

        # Pass 2: SKILL.md → legacy fallback
        for md_file in sorted(directory.rglob("SKILL.md")):
            pkg_dir = md_file.parent
            if pkg_dir in seen_dirs:
                continue
            # Skip if any ancestor directory already has a manifest.yaml
            # (a skill package's nested SKILL.md files must not override it)
            if any(parent in seen_dirs for parent in pkg_dir.parents):
                continue
            seen_dirs.add(pkg_dir)
            skill = load_legacy_skill_md(md_file, source=source)
            if skill and skill.name:
                # Legacy SKILL.md files must NOT override a skill that was
                # already loaded from a manifest.yaml (Pass 1 wins).
                if skill.name in self._skills:
                    continue
                self._skills[skill.name] = skill

    def get(self, name: str) -> SkillMetadata | None:
        if not self._loaded:
            self.load()
        return self._skills.get(name)

    def list_skills(self, category: str | None = None) -> list[SkillMetadata]:
        if not self._loaded:
            self.load()
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        return skills

    def list_categories(self) -> list[str]:
        if not self._loaded:
            self.load()
        return sorted(set(s.category for s in self._skills.values()))

    def search(self, query: str, limit: int = 10) -> list[SkillMetadata]:
        if not self._loaded:
            self.load()
        query_lower = query.lower()
        scored: list[tuple[int, SkillMetadata]] = []
        for skill in self._skills.values():
            score = 0
            if query_lower in skill.name.lower():
                score += 3
            if query_lower in skill.description.lower():
                score += 2
            for tag in skill.tags:
                if query_lower in tag.lower():
                    score += 1
            if skill.trigger and query_lower in skill.trigger.lower():
                score += 2
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    def get_skill_prompt(self, name: str) -> str | None:
        skill = self.get(name)
        if skill and skill.body:
            return skill.body
        return None

    def reload(self) -> dict[str, SkillMetadata]:
        self._skills.clear()
        self._loaded = False
        return self.load()


_registry: SkillsRegistry | None = None


def get_skills_registry() -> SkillsRegistry:
    global _registry
    if _registry is None:
        skills_dir = os.environ.get("ZHANLU_SKILLS_DIR", "skills")
        _registry = SkillsRegistry(skills_dir=skills_dir)
    return _registry


def get_skill(name: str) -> SkillMetadata | None:
    return get_skills_registry().get(name)


def get_skill_dir(name: str) -> str | None:
    """Return the absolute path to a skill's folder, or ``None`` if unknown.

    Used by the skills ``run`` action to locate the bundled ``scripts/``
    directory for sandbox execution. ``SkillMetadata.file_path`` points at
    the ``SKILL.md``; this returns its parent directory (resolved to an
    absolute path so it works whether the registry was loaded from a
    relative or absolute ``skills_dir``).
    """
    meta = get_skill(name)
    if meta is None or not meta.file_path:
        return None
    p = Path(meta.file_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p = p.resolve()
    except Exception:
        return None
    parent = p.parent
    return str(parent) if parent.exists() else None


def list_skill_scripts(name: str) -> list[dict]:
    """List runnable scripts in a skill's ``scripts/`` folder.

    Returns ``[{"name", "path", "size"}]`` for top-level files in the
    skill's ``scripts/`` directory. Empty list if the skill is unknown or
    has no ``scripts/`` folder. Used by the skills ``load``/``run`` actions
    so the agent knows which bundled entry points are executable.
    """
    d = get_skill_dir(name)
    if not d:
        return []
    scripts_dir = Path(d) / "scripts"
    if not scripts_dir.is_dir():
        return []
    out = []
    for p in sorted(scripts_dir.iterdir()):
        if p.is_file():
            try:
                out.append({
                    "name": p.name,
                    "path": f"scripts/{p.name}",
                    "size": p.stat().st_size,
                })
            except OSError:
                continue
    return out


def list_skills(category: str | None = None) -> list[SkillMetadata]:
    return get_skills_registry().list_skills(category)


def list_skill_categories() -> list[str]:
    """Return sorted list of all skill categories in the registry."""
    return get_skills_registry().list_categories()


def search_skills(query: str, limit: int = 10) -> list[SkillMetadata]:
    return get_skills_registry().search(query, limit)


def _lookup_skill_summary(name: str, db=None) -> str | None:
    """Look up the ``summary`` field for a skill (DB first, then FS fallback).

    Returns the summary string, or ``None`` if no summary is available.
    Falls back to the first 200 chars of the description if no explicit
    summary is stored.
    """
    registry = get_skills_registry()

    # 1. Try DB first
    if db is not None:
        try:
            from app.models.tool import Tool
            tool = db.query(Tool).filter(
                Tool.name == name,
                Tool.is_deleted == False,
                Tool.enabled == True,
            ).first()
            if tool and tool.summary:
                return tool.summary
            if tool and tool.description:
                return tool.description[:200]
        except Exception as e:
            logger.debug("DB summary lookup for '%s' failed (non-fatal): %s", name, e)

    # 2. Fallback to filesystem registry
    skill = registry.get(name)
    if skill:
        if skill.description:
            return skill.description[:200]
    return None


def get_skill_metadata_for_agent(skill_names: list[str], db=None) -> str:
    """Build a compact prompt section with only skill metadata (name + summary).

    This is the progressive-disclosure path: only metadata is injected into
    the system prompt. The agent calls ``load_skill_body(name)`` to fetch the
    full methodology body on demand.

    Args:
        skill_names: List of skill display names from ``AgentApp.skills``.
        db: Optional SQLAlchemy session.

    Returns:
        A compact markdown string listing available skills with their
        summaries, suitable for system-prompt injection.  Returns empty
        string if no skills are found.
    """
    if not skill_names:
        return ""

    registry = get_skills_registry()
    lines: list[str] = []
    count = 0

    for name in skill_names:
        summary = _lookup_skill_summary(name, db=db)
        if summary is None:
            # Skill not found in DB or FS — skip silently
            continue
        count += 1
        lines.append(f"- **{name}**: {summary}")

    if not lines:
        return ""

    header = (
        "## Available Skills\n"
        "You have the following skills available. Their full instructions "
        "are loaded on-demand. Call `load_skill_body(name)` to read a "
        "skill's complete methodology when needed:\n"
    )
    return header + "\n".join(lines)


def get_skill_prompt_for_agent(skill_names: list[str], db=None) -> str:
    """Build a prompt section with FULL skill body for injection into the
    agent system prompt (legacy path — used when progressive_disclosure is off).

    Reads from the DB ``tools`` table first (so user-created skills work),
    then falls back to the filesystem SkillsRegistry (for bundled marketplace
    skills that haven't been synced to DB yet).

    Args:
        skill_names: List of skill display names from ``AgentApp.skills``.
        db: Optional SQLAlchemy session.  If ``None``, only the filesystem
            registry is consulted.
    """
    sections: list[str] = []
    registry = get_skills_registry()

    for name in skill_names:
        body: str | None = None
        description: str | None = None

        # 1. Try DB first — user-created skills live here
        if db is not None:
            try:
                from app.models.tool import Tool
                tool = db.query(Tool).filter(
                    Tool.name == name,
                    Tool.is_deleted == False,
                    Tool.enabled == True,
                ).first()
                if tool and tool.skill_md:
                    body = tool.skill_md
                    description = tool.description or ""
            except Exception as e:
                logger.debug("DB skill lookup for '%s' failed (non-fatal): %s", name, e)

        # 2. Fallback to filesystem registry
        if body is None:
            skill = registry.get(name)
            if skill and skill.body:
                body = skill.body
                description = skill.description

        if body:
            sections.append(
                f"### Skill: {name}\n{description or ''}\n\n{body}"
            )

    if not sections:
        return ""
    return "## Loaded Skills\n\n" + "\n\n---\n\n".join(sections)


def unified_search(query: str, limit: int = 10, db=None) -> list[dict[str, Any]]:
    """Search skills across both the DB ``tools`` table and the filesystem registry.

    Returns a list of dicts with keys: ``name``, ``description``, ``category``,
    ``trigger``, ``source`` ("db" or "filesystem"), and ``kind``.
    """
    query_lower = query.lower()
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    # 1. Search DB tools table
    if db is not None:
        try:
            from app.models.tool import Tool
            tools = db.query(Tool).filter(
                Tool.is_deleted == False,
                Tool.enabled == True,
                Tool.name.ilike(f"%{query}%") | Tool.description.ilike(f"%{query}%"),
            ).limit(limit * 2).all()
            for t in tools:
                if t.name in seen:
                    continue
                seen.add(t.name)
                results.append({
                    "name": t.name,
                    "description": t.description or "",
                    "category": t.category or "",
                    "trigger": t.trigger or "",
                    "source": "db",
                    "kind": t.kind or "system_skill",
                })
        except Exception as e:
            logger.debug("DB skill search failed (non-fatal): %s", e)

    # 2. Search filesystem registry
    fs_skills = get_skills_registry().search(query, limit=limit * 2)
    for skill in fs_skills:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        results.append({
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "trigger": skill.trigger,
            "source": "filesystem",
            "kind": "system_skill",
        })

    return results[:limit]


def get_or_install_skill(
    name: str,
    category: str = "",
    db=None,
) -> SkillMetadata | None:
    """Retrieve a skill by name, auto-installing from the bundled marketplace if missing.

    Resolution order (three-tier):
      1. Filesystem: ``~/.zhanlu/skills/<category>/<name>/SKILL.md``
      2. Database:  ``tools`` table row (checked via ``unified_search``)
      3. Bundled marketplace: copy from ``backend/skills/<name>/SKILL.md``
         via ``skill_sync.write_skill_md()`` + ``reload_skills_registry()``

    Args:
        name: Skill display name (e.g. "docx", "pptx").
        category: Optional category hint for filesystem lookup.
        db: Optional SQLAlchemy session for DB-tier lookup.

    Returns:
        The resolved ``SkillMetadata``, or ``None`` if the skill cannot be found.
    """
    import os as _os
    from pathlib import Path as _Path

    registry = get_skills_registry()

    # --- Tier 1: Filesystem (~/.zhanlu/skills) ---
    skill = registry.get(name)
    if skill is not None:
        return skill

    # --- Tier 2: DB tools table ---
    if db is not None:
        try:
            results = unified_search(name, limit=1, db=db)
            if results:
                matched_name = results[0]["name"]
                # Re-check filesystem in case DB had it but registry missed it
                skill = registry.get(matched_name)
                if skill is not None:
                    return skill
                # DB has metadata but not in filesystem — try to restore from bundled
                name = matched_name
        except Exception as e:
            logger.debug("DB lookup for skill '%s' failed (non-fatal): %s", name, e)

    # --- Tier 3: Bundled marketplace (backend/skills/<name>/SKILL.md) ---
    try:
        from app.services import skill_sync

        # Search for a SKILL.md under backend/skills/ whose parent dir matches the name.
        bundled_skills_dir = _Path(__file__).resolve().parents[3] / "skills"
        if bundled_skills_dir.exists():
            for md_file in sorted(bundled_skills_dir.rglob("SKILL.md")):
                parsed = parse_skill_file(md_file, source="bundled")
                if parsed and parsed.name.lower() == name.lower():
                    # Found it — copy into ~/.zhanlu/skills
                    write_path = skill_sync.write_skill_md(
                        name=parsed.name,
                        description=parsed.description,
                        body=parsed.body,
                        category=category or parsed.category or "doc",
                        trigger=parsed.trigger,
                        version=parsed.version,
                        author=parsed.author or "marketplace",
                        tags=parsed.tags,
                    )
                    logger.info(
                        "Auto-installed skill %r from bundled marketplace: %s",
                        name, write_path,
                    )
                    skill_sync.reload_skills_registry()

                    # Re-lookup after reload
                    skill = registry.get(parsed.name)
                    if skill is not None:
                        return skill
                    # Fallback: re-check by the safe-name we wrote
                    skill = registry.get(name)
                    if skill is not None:
                        return skill
                    break

        # Broader search: try any bundled skill whose name contains the query
        for md_file in sorted(bundled_skills_dir.rglob("SKILL.md")):
            parsed = parse_skill_file(md_file, source="bundled")
            if parsed and name.lower() in parsed.name.lower():
                write_path = skill_sync.write_skill_md(
                    name=parsed.name,
                    description=parsed.description,
                    body=parsed.body,
                    category=category or parsed.category or "general",
                    trigger=parsed.trigger,
                    version=parsed.version,
                    author=parsed.author or "marketplace",
                    tags=parsed.tags,
                )
                logger.info(
                    "Auto-installed skill %r (fuzzy match) from bundled marketplace: %s",
                    parsed.name, write_path,
                )
                skill_sync.reload_skills_registry()
                return registry.get(parsed.name)
    except Exception as e:
        logger.warning("Auto-install of skill %r failed (non-fatal): %s", name, e)

    return None


__all__ = [
    "SkillMetadata",
    "SkillsRegistry",
    "parse_frontmatter",
    "parse_manifest",
    "validate_manifest",
    "parse_skill_file",
    "load_skill_package",
    "load_legacy_skill_md",
    "get_skills_registry",
    "get_skill",
    "list_skills",
    "search_skills",
    "get_skill_prompt_for_agent",
    "get_skill_metadata_for_agent",
    "unified_search",
    "get_or_install_skill",
]

# ----------------------------------------------------------------------
# Synexia / Phase-B additions
# ----------------------------------------------------------------------
# These re-exports add the discovery + planner-hook layer to the
# existing skill loader so the SynexiaFSM planner can list every
# skill by name and lazily fetch SKILL.md bodies.  Existing
# ``parse_manifest`` / ``SkillsRegistry`` / ``SkillMetadata`` etc. are
# still exported above.
try:  # pragma: no cover — defensive
    from app.services.skills_loader.manifest_index import (  # noqa: F401
        ManifestIndex as _ManifestIndex,
        SkillManifest as _SkillManifest,
        get_manifest_index as _get_manifest_index,
    )
    from app.services.skills_loader.skill_planner_hook import (  # noqa: F401
        LoadSkillResult as _LoadSkillResult,
        SkillPlannerHook as _SkillPlannerHook,
        get_skill_planner_hook as _get_skill_planner_hook,
    )
except Exception:  # pragma: no cover
    pass

# Re-export under the canonical names.  The try/except above makes
# import-time safe even if a submodule is unavailable for any reason.
from app.services.skills_loader.manifest_index import (  # noqa: E402,F401
    ManifestIndex,
    SkillManifest,
    get_manifest_index,
)
from app.services.skills_loader.skill_planner_hook import (  # noqa: E402,F401
    LoadSkillResult,
    SkillPlannerHook,
    get_skill_planner_hook,
)
