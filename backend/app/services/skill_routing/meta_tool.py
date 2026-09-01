"""Skill meta-tool — a single LLM-callable ``Skill`` dispatcher.

Registers one ``Skill`` tool that accepts ``{command: string}`` (bare or
namespaced, e.g. ``"pptx"`` or ``"user:my-template-ppt"``).

Actions (dispatched from *command*):
- ``load <name>``   → return full SKILL.md body (Layer B)
- ``execute <name>`` → inject skill context for this turn
- bare name         → load (convenience alias)

The tool's ``description`` is rebuilt each turn with the progressive-
disclosure catalog from ``build_catalog``.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

# Tool names the LLM sometimes mistakes for skill names when invoking the
# ``Skill`` dispatcher (e.g. ``Skill {"command": "load_skill_body"}``). When
# detected, we return a self-correcting redirect instead of a dead-end
# "Skill not found" error.
_TOOL_NAMES_MISTAKEN_AS_SKILLS = frozenset({
    "load_skill_body",
    "skills",
    "list_default_skills",
    "skill",
    "ask_data_agent",
    "ask_forecast_agent",
    "ask_forecast",
    "ask_report_agent",
    "ask_rag",
    "ask_knowledge_graph",
    "ask_decision",
    "ask_macro_override",
    "ask_weekly_report",
    "execute_query",
    "describe_schema",
    "web_search",
})

# ── The Skill meta-tool handler ─────────────────────────────────────────


async def _skill_meta_tool(
    args: dict,
    db: Optional = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Dispatch a ``Skill`` command: load or execute a skill by name."""
    command: str = (args.get("command") or "").strip()

    if not command:
        return {"success": False, "error": "command is required (e.g. 'pptx' or 'user:my-ppt')"}

    # Strip optional action prefixes
    action, skill_name = _parse_action(command)

    # Bare action prefix (e.g. `Skill {"command": "execute"}`) — the LLM
    # invoked the action without a skill name. Return an instructive error
    # instead of the dead-end "Skill not found: 'execute'".
    if command in ("execute", "load"):
        return {
            "success": False,
            "error": (
                f"'{command}' is an action prefix, not a skill name. "
                "You must include the skill name after the action. "
                "Call: Skill {\"command\": \"execute <skill-name>\"} "
                "(e.g. 'execute weekly-report-generation')."
            ),
        }

    # Self-correcting redirect: the LLM sometimes passes a *tool* name as the
    # skill name (e.g. `Skill {"command": "load_skill_body"}`), confusing the
    # Skill dispatcher with the underlying tool. Detect that and tell the LLM
    # exactly what to do instead of returning a dead-end "Skill not found".
    if skill_name in _TOOL_NAMES_MISTAKEN_AS_SKILLS:
        return {
            "success": False,
            "error": (
                f"'{skill_name}' is a tool name, not a skill name. "
                "To activate a skill, call: Skill {\"command\": \"execute <skill-name>\"} "
                f"(e.g. 'execute weekly-report-generation'). "
                f"To call the '{skill_name}' tool, invoke it directly by its own "
                "name — do NOT pass it as a Skill command."
            ),
        }

    try:
        from app.services.skills_loader import get_skill, unified_search
    except ImportError as exc:
        return {"success": False, "error": f"skills_loader unavailable: {exc}"}

    try:
        meta = get_skill(skill_name)
        if meta is None:
            # Try fuzzy search as fallback
            candidates = unified_search(skill_name, limit=5, db=db)
            if candidates:
                first = candidates[0]
                # Re-try with the first match
                meta = get_skill(first.get("name", ""))
    except Exception as exc:
        # Never let a loader/registry failure surface as a crashed tool
        # call — the LLM needs a readable error so it can continue the turn
        # (e.g. fall back to the deterministic export path) instead of the
        # runtime marking the step failed and derailing the stream.
        logger.warning("Skill meta-tool: lookup of %r failed: %s", skill_name, exc)
        return {
            "success": False,
            "error": (
                f"Skill lookup failed for {skill_name!r}: {exc}. "
                "Continue without the skill — if the user asked for a file, "
                "the server-side export pipeline will still produce it."
            ),
        }
    if meta is None:
        return {"success": False, "error": f"Skill not found: {skill_name!r}"}

    if action == "execute":
        return {
            "success": True,
            "name": meta.name,
            "action": "execute",
            "instruction": (
                f"Skill {meta.name!r} is now active for this turn. "
                f"Follow the methodology in the SKILL.md content below."
            ),
            "skill_content": (meta.body or "")[:8000],
            "runtime": getattr(meta, "runtime", None),
        }

    # Default: load
    return {
        "success": True,
        "name": meta.name,
        "action": "load",
        "description": meta.description,
        "summary": getattr(meta, "summary", ""),
        "category": getattr(meta, "category", ""),
        "tags": getattr(meta, "tags", []) or [],
        "content": meta.body,  # full body — Layer B
        "source": getattr(meta, "source", "builtin"),
    }


def _parse_action(command: str) -> tuple[str, str]:
    """Split optional action prefix from the skill name.

    >>> _parse_action("load pptx")
    ('load', 'pptx')
    >>> _parse_action("execute user:my-ppt")
    ('execute', 'user:my-ppt')
    >>> _parse_action("pptx")
    ('load', 'pptx')
    """
    parts = command.split(maxsplit=1)
    if len(parts) == 2 and parts[0] in ("load", "execute"):
        return parts[0], parts[1]
    return "load", command


# ── Dynamic description builder ─────────────────────────────────────────


def _build_description(context: Optional[dict] = None) -> str:
    """Build the tool description with the current catalog injected."""
    base = (
        "Unified Skill dispatcher. Invoke with {command: '<name>'} "
        "(bare name like 'pptx' or fully-qualified like 'user:my-ppt').\n"
        "Use 'load <name>' to read a skill, 'execute <name>' to activate it."
    )

    # Try to inject the token-budgeted catalog
    try:
        from app.services.skill_routing.catalog import build_catalog
        from app.services.skills_loader import list_skills as loader_list_skills

        all_skills = [s.to_dict() for s in loader_list_skills()]
        catalog_block = build_catalog(all_skills)
        if catalog_block:
            base += "\n\n<available_skills>\n" + catalog_block + "\n</available_skills>"
    except Exception:
        logger.debug("Could not build skill catalog for tool description", exc_info=True)

    return base


# ── Registration ────────────────────────────────────────────────────────


SKILL_META_SCHEMA = {
    "type": "function",
    "function": {
        "name": "Skill",
        "description": _build_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Skill name (bare or namespaced). "
                        "Examples: 'pptx', 'builtin:pptx', 'user:my-template', "
                        "'load pptx', 'execute user:my-ppt'."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}


def register_skill_meta_tool():
    """Register the ``Skill`` meta-tool with the tool registry.

    Call once at app startup (after skills_loader is populated).
    Returns ``True`` on success, ``False`` if already registered.
    """
    try:
        # Refresh the dynamic description
        SKILL_META_SCHEMA["function"]["description"] = _build_description()
        registry.register(
            name="Skill",
            schema=SKILL_META_SCHEMA,
            handler=_skill_meta_tool,
            category="skills",
            toolset="skills",
            description="Unified Skill dispatcher: load and execute skills by name.",
            emoji="🎯",
        )
        logger.info("Skill meta-tool registered")
        return True
    except Exception:
        logger.warning("Skill meta-tool registration failed", exc_info=True)
        return False
