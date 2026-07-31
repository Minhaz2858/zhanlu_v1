"""skill_manager tool — install/uninstall user skills from disk.

Hermes' skill_manager handles marketplace + local installs. In zhanlu
we have the existing ``app.services.skill_sync.write_skill_md`` for
writing skills to the filesystem; this tool exposes a clean install
uninstall surface for the agent.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _user_skills_dir() -> Path:
    return Path.home() / ".zhanlu" / "skills"


async def _skill_manager(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()

    if action == "list":
        base = _user_skills_dir()
        if not base.exists():
            return {"success": True, "skills": []}
        out = []
        for category_dir in sorted(base.iterdir()):
            if not category_dir.is_dir():
                continue
            for skill_dir in sorted(category_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    out.append({
                        "name": skill_dir.name,
                        "category": category_dir.name,
                        "path": str(skill_md),
                        "size_bytes": skill_md.stat().st_size,
                    })
        return {"success": True, "skills": out}

    if action == "install":
        name = (args.get("name") or "").strip()
        content = args.get("content", "")
        category = (args.get("category") or "custom").strip()
        if not name or not content:
            return {"success": False, "error": "name and content are required"}
        try:
            from app.services.skill_sync import write_skill_md, reload_skills_registry
            path = write_skill_md(
                name=name,
                description=args.get("description", ""),
                body=content,
                category=category,
                trigger=args.get("trigger", ""),
                author=user_id or "user",
            )
            reload_skills_registry()
            return {"success": True, "path": path, "name": name, "category": category}
        except Exception as exc:
            return {"success": False, "error": f"Install failed: {exc}"}

    if action == "uninstall":
        name = (args.get("name") or "").strip()
        category = (args.get("category") or "custom").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        target = _user_skills_dir() / category / name
        if not target.exists():
            return {"success": False, "error": f"Skill not found: {category}/{name}"}
        try:
            shutil.rmtree(target)
            from app.services.skill_sync import reload_skills_registry
            reload_skills_registry()
            return {"success": True, "removed": str(target)}
        except Exception as exc:
            return {"success": False, "error": f"Uninstall failed: {exc}"}

    return {"success": False, "error": f"Unknown action: {action!r}"}


SKILL_MANAGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_manager",
        "description": (
            "Install or uninstall user skills from the filesystem. "
            "Install writes a SKILL.md under ~/.zhanlu/skills/<category>/<name>/ "
            "and reloads the registry. Uninstall removes the directory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "install", "uninstall"]},
                "name": {"type": "string", "description": "Skill name."},
                "category": {"type": "string", "description": "Skill category.", "default": "custom"},
                "content": {"type": "string", "description": "Full SKILL.md body (for install)."},
                "description": {"type": "string", "description": "Skill description (for install)."},
                "trigger": {"type": "string", "description": "Trigger phrase (for install)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="skill_manager",
    schema=SKILL_MANAGER_SCHEMA,
    handler=_skill_manager,
    category="skills",
    toolset="skills",
    description="Install/uninstall user skills from the filesystem.",
    emoji="📥",
    max_result_size_chars=30_000,
)
