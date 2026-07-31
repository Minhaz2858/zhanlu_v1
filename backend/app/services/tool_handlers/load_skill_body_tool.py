"""load_skill_body tool — fetch full skill body on demand.

Progressive-disclosure companion: when ``progressive_disclosure`` is enabled
on an agent, only name+description+summary go into the system prompt.  The
agent calls this tool to load a skill's complete methodology when needed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _load_skill_body(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Load the full methodology body for a skill by name.

    Looks up the skill in the DB ``tools`` table first (reads ``skill_md``),
    then falls back to the filesystem SkillsRegistry.

    Returns the full body or an error if the skill is not found / has no body.
    """
    from app.services.skill_execution_recorder import SkillExecutionRecorder

    name = (args.get("name") or "").strip()
    if not name:
        return {"success": False, "error": "name is required"}

    _start = time.monotonic()
    body: str | None = None
    description: str | None = None

    # 1. Try DB — user-created and marketplace skills live here with skill_md
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
            logger.debug("DB lookup for '%s' failed (non-fatal): %s", name, e)

    # 2. Fallback to filesystem registry
    if body is None:
        try:
            from app.services.skills_loader import get_skills_registry
            reg = get_skills_registry()
            skill = reg.get(name)
            if skill and skill.body:
                body = skill.body
                description = skill.description
        except Exception as e:
            logger.debug("FS lookup for '%s' failed (non-fatal): %s", name, e)

    _duration_ms = int((time.monotonic() - _start) * 1000)

    if body is None:
        SkillExecutionRecorder.record_from_context(
            skill_name=name,
            action="load",
            status="failed",
            context=context,
            duration_ms=_duration_ms,
            error_message=f"Skill '{name}' not found or has no body",
        )
        return {"success": False, "error": f"Skill '{name}' not found or has no body"}

    SkillExecutionRecorder.record_from_context(
        skill_name=name,
        action="load",
        status="completed",
        context=context,
        duration_ms=_duration_ms,
        output_json={"body_length": len(body)},
    )

    return {
        "success": True,
        "name": name,
        "description": description or "",
        "body": body,
    }


LOAD_SKILL_BODY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_skill_body",
        "description": (
            "Load the full methodology instructions for a skill by name. "
            "Use this when you need a skill's complete step-by-step guide, "
            "tool references, or detailed workflows. The skill's summary "
            "in your system prompt should help you decide when to call this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The exact skill name (e.g. 'pdf', 'pptx', 'docx').",
                },
            },
            "required": ["name"],
        },
    },
}

registry.register(
    name="load_skill_body",
    schema=LOAD_SKILL_BODY_SCHEMA,
    handler=_load_skill_body,
    category="skills",
    toolset="skills",
    description="Load full methodology instructions for a skill on demand.",
    emoji="📖",
    max_result_size_chars=50_000,
)
