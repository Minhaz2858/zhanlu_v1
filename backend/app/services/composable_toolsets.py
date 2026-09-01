"""Composable toolsets -- posture-based tool selection with recursive resolution.

Toolsets can include other toolsets, be resolved recursively, and be
selected by "posture" (coding, research, safe). This replaces the flat
``tool_config`` per agent with a more flexible, composable system.

A posture is a named preset that defines which tools are available:
- ``coding``: write_file, read_file, execute_code, web_search, etc.
- ``research``: read_file, web_search, web_extract, ask_data_agent, etc.
- ``safe``: read_file, web_search, list_tools (read-only, no mutations)

Toolsets can also include other toolsets (recursive composition).

Falls back to the existing ``tool_config`` when no posture is set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Toolset:
    """A named collection of tools that can include other toolsets.

    Attributes:
        name: Unique toolset name.
        tools: Set of tool names in this toolset.
        includes: Names of other toolsets to include (resolved recursively).
        description: Human-readable description.
    """
    name: str
    tools: frozenset[str] = field(default_factory=frozenset)
    includes: list[str] = field(default_factory=list)
    description: str = ""


# -- Built-in posture toolsets --

_CODING_TOOLS = frozenset({
    "read_file", "write_file", "execute_code", "web_search", "web_extract",
    "list_tools", "list_market_agents", "list_knowledge_bases", "memory",
    "create_artifact", "ask_data_agent",
})

_RESEARCH_TOOLS = frozenset({
    "read_file", "web_search", "web_extract", "ask_data_agent",
    "list_tools", "list_knowledge_bases", "search_skills", "skills",
    "create_artifact",
})

_SAFE_TOOLS = frozenset({
    "read_file", "web_search", "web_extract", "list_tools",
    "list_market_agents", "list_knowledge_bases", "search_skills",
})

# -- Toolset registry --

_REGISTRY: dict[str, Toolset] = {
    "coding": Toolset(
        name="coding",
        tools=_CODING_TOOLS,
        description="Full development toolset: file I/O, code execution, web, data, memory",
    ),
    "research": Toolset(
        name="research",
        tools=_RESEARCH_TOOLS,
        description="Research toolset: read-only + data + web (no file writes or code execution)",
    ),
    "safe": Toolset(
        name="safe",
        tools=_SAFE_TOOLS,
        description="Safe read-only toolset: no mutations, no code execution",
    ),
}


def register_toolset(toolset: Toolset) -> None:
    """Register a custom toolset."""
    _REGISTRY[toolset.name] = toolset
    logger.info("Registered toolset: %s (%d tools)", toolset.name, len(toolset.tools))


def get_toolset(name: str) -> Toolset | None:
    """Get a toolset by name."""
    return _REGISTRY.get(name)


def list_toolsets() -> list[str]:
    """List all registered toolset names."""
    return sorted(_REGISTRY.keys())


def resolve_tools(
    toolset_names: list[str],
    *,
    extra_tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
) -> list[str]:
    """Recursively resolve tool names from one or more toolsets.

    Args:
        toolset_names: Names of toolsets to resolve (includes are followed recursively).
        extra_tools: Additional tool names to include.
        exclude_tools: Tool names to exclude (overrides everything).

    Returns:
        Sorted list of unique tool names.
    """
    resolved: set[str] = set()
    visited: set[str] = set()

    def _resolve(name: str, depth: int = 0) -> None:
        if name in visited or depth > 10:  # prevent infinite recursion
            return
        visited.add(name)

        toolset = _REGISTRY.get(name)
        if toolset is None:
            logger.warning("Unknown toolset: %s", name)
            return

        resolved.update(toolset.tools)

        for included in toolset.includes:
            _resolve(included, depth + 1)

    for ts_name in toolset_names:
        _resolve(ts_name)

    if extra_tools:
        resolved.update(extra_tools)

    if exclude_tools:
        resolved -= set(exclude_tools)

    return sorted(resolved)


def resolve_from_agent_config(
    tool_config: dict[str, Any] | None,
    *,
    posture: str | None = None,
    skills: list[str] | None = None,
) -> list[str]:
    """Resolve tools from agent config, posture, and skills.

    Priority:
    1. If ``posture`` is set, resolve from the posture toolset.
    2. If ``tool_config`` has ``enabled_tools``, use those directly.
    3. If ``skills`` are set, resolve tools from skills.
    4. Fall back to the "coding" posture (most permissive).

    Args:
        tool_config: The agent's ``tool_config`` JSON (may have ``enabled_tools``).
        posture: Optional posture name ("coding", "research", "safe").
        skills: Optional list of skill names to derive tools from.

    Returns:
        Sorted list of tool names.
    """
    # 1. Posture takes precedence
    if posture and posture in _REGISTRY:
        extra = []
        if tool_config and isinstance(tool_config.get("enabled_tools"), list):
            extra = tool_config["enabled_tools"]
        return resolve_tools([posture], extra_tools=extra)

    # 2. Explicit enabled_tools
    if tool_config and isinstance(tool_config.get("enabled_tools"), list):
        return sorted(set(tool_config["enabled_tools"]))

    # 3. Skills-based resolution
    if skills:
        try:
            from app.services.tool_registry import resolve_tools_from_skills
            return resolve_tools_from_skills(skills)
        except Exception:
            pass

    # 4. Default: coding posture
    return resolve_tools(["coding"])


__all__ = [
    "Toolset",
    "register_toolset",
    "get_toolset",
    "list_toolsets",
    "resolve_tools",
    "resolve_from_agent_config",
]
