"""A2A AgentCard — machine-readable agent discovery (A2A v1.0).

A card describes one Zhanlu agent so external agents can discover it and
delegate tasks to it. Served at ``GET /a2a/agent-card``.
"""
from __future__ import annotations

from typing import Any

CARD_VERSION = "1.0.0"


def _skill_from_capability(capability: str) -> dict[str, Any]:
    return {
        "id": f"capability-{len(capability.encode('utf-8'))}",
        "name": capability,
        "description": f"Zhanlu capability: {capability}",
        "tags": ["zhanlu", "capability"],
    }


def build_agent_card(agent: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Build an A2A AgentCard from an AgentApp-like dict.

    ``agent`` needs at least ``id``/``name``; optional ``description`` and
    ``capabilities`` (list). Capabilities surface as card skills.
    """
    capabilities = agent.get("capabilities") or []
    skills = [_skill_from_capability(c) for c in capabilities if isinstance(c, str)]
    if not skills:
        skills = [{
            "id": f"agent-{agent.get('id', 'unknown')}",
            "name": agent.get("name", "zhanlu-agent"),
            "description": agent.get("description") or "Zhanlu enterprise agent",
            "tags": ["zhanlu"],
        }]
    return {
        "name": agent.get("name", "zhanlu"),
        "description": agent.get("description") or "Zhanlu enterprise AI agent",
        "url": f"{base_url.rstrip('/')}/a2a",
        "version": CARD_VERSION,
        "skills": skills,
        "authentication": {"schemes": ["bearer"]},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


def build_platform_card(agents: list[dict[str, Any]], base_url: str) -> dict[str, Any]:
    """Aggregate card exposing every published agent as a skill."""
    return {
        "name": "zhanlu",
        "description": "Zhanlu Enterprise AI OS — external delegation hub",
        "url": f"{base_url.rstrip('/')}/a2a",
        "version": CARD_VERSION,
        "skills": [
            {
                "id": f"agent-{a.get('id', 'unknown')}",
                "name": a.get("name", "agent"),
                "description": a.get("description") or "Zhanlu enterprise agent",
                "tags": ["zhanlu", "agent"],
            }
            for a in agents
        ],
        "authentication": {"schemes": ["bearer"]},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


__all__ = ["build_agent_card", "build_platform_card"]
