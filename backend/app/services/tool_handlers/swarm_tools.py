"""Swarm coordination tools — exposes the swarm as agent-callable tools.

These let any agent in the ReAct loop:
- ``swarm_create_team``        — create a new swarm team
- ``swarm_spawn_agent``       — spawn a sub-agent inside a team
- ``swarm_send_message``      — post a message to a team member's mailbox
- ``swarm_get_messages``      — read (and clear) a member's mailbox
- ``swarm_list_teams``        — enumerate active teams

All tools are registered on import via ``registry.register(...)`` so the
runtime picks them up automatically.

Note: ``registry`` here is intentionally a module-level name imported
lazily at call time, so this module is safe to import without side
effects (no DB / LLM imports at import time).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas — OpenAI function-calling format
# ---------------------------------------------------------------------------

_CREATE_TEAM_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_create_team",
        "description": "Create a new swarm team. Teams are containers for spawned agents that share a mailbox. Returns the new team's id.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable team name."},
                "description": {"type": "string", "description": "Optional description of the team's purpose."},
            },
            "required": ["name"],
        },
    },
}

_SPAWN_AGENT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_spawn_agent",
        "description": "Spawn a sub-agent inside a swarm team. The sub-agent runs the named agent definition (e.g. 'general-purpose', 'worker') on the given task in the background. Returns the member name.",
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "The id of the team to spawn into."},
                "agent_name": {"type": "string", "description": "The agent definition name to use (e.g. 'general-purpose', 'explore', 'worker', 'plan', 'verification')."},
                "task": {"type": "string", "description": "The natural-language task description."},
                "member_name": {"type": "string", "description": "Optional display name. Defaults to 'agent-N'."},
            },
            "required": ["team_id", "agent_name", "task"],
        },
    },
}

_SEND_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_send_message",
        "description": "Send a message to a swarm team member's mailbox. The message is persisted to the database so it survives team-registry restarts.",
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "recipient": {"type": "string", "description": "Member name, or 'main' to send to the team lead."},
                "content": {"type": "string"},
                "summary": {"type": "string", "description": "Optional short summary (<= 500 chars)."},
                "priority": {"type": "integer", "description": "Message priority. Higher = dequeued first. Default 0.", "default": 0},
            },
            "required": ["team_id", "recipient", "content"],
        },
    },
}

_GET_MESSAGES_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_get_messages",
        "description": "Read and clear the mailbox messages for a team member. Returns the list of pending messages.",
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "member_name": {"type": "string"},
            },
            "required": ["team_id", "member_name"],
        },
    },
}

_LIST_TEAMS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_list_teams",
        "description": "List all active swarm teams and their members.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_SCRATCH_SET_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_scratch_set",
        "description": (
            "Write a value to the team's shared scratchpad. Parallel workers "
            "use this to share fetched context / intermediate results so they "
            "don't redundantly re-fetch the same data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "key": {"type": "string", "description": "Scratchpad key."},
                "value": {"type": "string", "description": "Value to store."},
            },
            "required": ["team_id", "key", "value"],
        },
    },
}

_SCRATCH_GET_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_scratch_get",
        "description": "Read a value from the team's shared scratchpad (returns null if absent).",
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["team_id", "key"],
        },
    },
}

_ORCHESTRATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_orchestrate",
        "description": (
            "Run a list of subtasks with result-driven re-dispatch: each subtask "
            "is retried on failure (with the failure context appended) and "
            "escalated to a stronger agent on the final retry. An aggregated "
            "summary is posted to the team lead's mailbox. Runs in the "
            "background — poll the mailbox for results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "description": "Subtasks to run with re-dispatch.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_name": {"type": "string", "description": "Agent definition to use (e.g. 'worker', 'general-purpose')."},
                            "task": {"type": "string"},
                            "member_name": {"type": "string"},
                        },
                        "required": ["agent_name", "task"],
                    },
                },
                "max_retries": {"type": "integer", "description": "Re-dispatch attempts per subtask (default 1).", "default": 1},
                "escalate_to": {"type": "string", "description": "Agent to escalate to on the final retry (default 'general-purpose').", "default": "general-purpose"},
            },
            "required": ["team_id", "tasks"],
        },
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _registry():
    """Lazy import so this module has no side effects at import time."""
    from app.services.tool_registry import registry
    return registry


def _handle_create_team(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    name = (arguments.get("name") or "").strip()
    if not name:
        return {"success": False, "error": "name is required"}
    description = arguments.get("description") or ""
    team = get_team_registry().create_team(name=name, description=description)
    return {"success": True, "team_id": team.id, "name": team.name}


def _handle_spawn_agent(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_swarm_coordinator
    team_id = arguments.get("team_id")
    agent_name = arguments.get("agent_name")
    task = arguments.get("task") or ""
    member_name = arguments.get("member_name")
    if not (team_id and agent_name and task):
        return {"success": False, "error": "team_id, agent_name, task are required"}

    async def _go():
        return await get_swarm_coordinator().spawn_agent(
            team_id=team_id,
            agent_name=agent_name,
            task=task,
            member_name=member_name,
            db=db,
            user_id=user_id,
        )

    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an event loop — fire-and-forget the spawn
            asyncio.create_task(_go())
            return {"success": True, "spawned": True, "note": "agent started in background"}
        else:
            member = loop.run_until_complete(_go())
    except RuntimeError:
        member = asyncio.run(_go())
    return {"success": True, "member_name": member}


def _handle_send_message(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    team_id = arguments.get("team_id")
    sender = arguments.get("sender") or "main"
    recipient = arguments.get("recipient")
    content = arguments.get("content") or ""
    summary = arguments.get("summary") or ""
    if not (team_id and recipient and content):
        return {"success": False, "error": "team_id, recipient, content are required"}
    try:
        priority = int(arguments.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    ok = get_team_registry().send_message(
        team_id, sender, recipient, content, summary=summary, priority=priority,
    )
    return {"success": ok}


def _handle_get_messages(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    team_id = arguments.get("team_id")
    member_name = arguments.get("member_name")
    if not (team_id and member_name):
        return {"success": False, "error": "team_id and member_name are required"}
    messages = get_team_registry().get_messages(team_id, member_name)
    return {
        "success": True,
        "messages": [
            {
                "id": m.get("id"),
                "sender": m.get("sender"),
                "recipient": m.get("recipient"),
                "content": m.get("content"),
                "summary": m.get("summary"),
                "timestamp": m.get("timestamp") or m.get("created_date"),
            }
            for m in messages
        ],
        "count": len(messages),
    }


def _handle_list_teams(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    teams = get_team_registry().list_teams()
    return {
        "success": True,
        "teams": [t.to_dict() for t in teams],
        "count": len(teams),
    }


def _handle_scratch_set(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    team_id = arguments.get("team_id")
    key = arguments.get("key")
    value = arguments.get("value")
    if not (team_id and key and value is not None):
        return {"success": False, "error": "team_id, key, value are required"}
    ok = get_team_registry().set_scratch(team_id, key, str(value))
    return {"success": ok}


def _handle_scratch_get(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_team_registry
    team_id = arguments.get("team_id")
    key = arguments.get("key")
    if not (team_id and key):
        return {"success": False, "error": "team_id and key are required"}
    value = get_team_registry().get_scratch(team_id, key)
    return {"success": True, "key": key, "value": value, "found": value is not None}


def _handle_orchestrate(arguments: dict, db, user_id) -> dict:
    from app.services.swarm.team_registry import get_swarm_coordinator
    from app.services.swarm.orchestrator import (
        SwarmOrchestrator, OrchestratedTask, OrchestrationPolicy,
    )

    team_id = arguments.get("team_id")
    raw_tasks = arguments.get("tasks") or []
    if not (team_id and isinstance(raw_tasks, list) and raw_tasks):
        return {"success": False, "error": "team_id and a non-empty tasks array are required"}

    try:
        max_retries = int(arguments.get("max_retries", 1))
    except (TypeError, ValueError):
        max_retries = 1
    escalate_to = arguments.get("escalate_to") or "general-purpose"
    policy = OrchestrationPolicy(max_retries=max_retries, escalate_to=escalate_to)

    tasks = [
        OrchestratedTask(
            agent_name=str(t.get("agent_name") or "worker"),
            task=str(t.get("task") or ""),
            member_name=t.get("member_name"),
        )
        for t in raw_tasks if isinstance(t, dict)
    ]
    if not tasks:
        return {"success": False, "error": "no valid tasks parsed"}

    orchestrator = SwarmOrchestrator(get_swarm_coordinator())

    async def _go():
        return await orchestrator.orchestrate(team_id, tasks, policy=policy)

    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_go())  # fire-and-forget; summary → mailbox
            return {"success": True, "spawned": True, "task_count": len(tasks),
                    "note": "orchestration started in background; results posted to mailbox"}
        loop.run_until_complete(_go())
    except RuntimeError:
        asyncio.run(_go())
    return {"success": True, "task_count": len(tasks),
            "note": "orchestration completed; results posted to mailbox"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_swarm_tools() -> None:
    """Register all swarm tools with the tool registry.

    Safe to call multiple times — registration overwrites by name.
    """
    reg = _registry()
    reg.register(
        name="swarm_create_team",
        schema=_CREATE_TEAM_SCHEMA,
        handler=_handle_create_team,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_CREATE_TEAM_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_spawn_agent",
        schema=_SPAWN_AGENT_SCHEMA,
        handler=_handle_spawn_agent,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_SPAWN_AGENT_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_send_message",
        schema=_SEND_MESSAGE_SCHEMA,
        handler=_handle_send_message,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_SEND_MESSAGE_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_get_messages",
        schema=_GET_MESSAGES_SCHEMA,
        handler=_handle_get_messages,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_GET_MESSAGES_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_list_teams",
        schema=_LIST_TEAMS_SCHEMA,
        handler=_handle_list_teams,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_LIST_TEAMS_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_scratch_set",
        schema=_SCRATCH_SET_SCHEMA,
        handler=_handle_scratch_set,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_SCRATCH_SET_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_scratch_get",
        schema=_SCRATCH_GET_SCHEMA,
        handler=_handle_scratch_get,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_SCRATCH_GET_SCHEMA["function"]["description"],
    )
    reg.register(
        name="swarm_orchestrate",
        schema=_ORCHESTRATE_SCHEMA,
        handler=_handle_orchestrate,
        category="swarm",
        toolset="swarm",
        is_async=False,
        description=_ORCHESTRATE_SCHEMA["function"]["description"],
    )


# Auto-register on import
register_swarm_tools()
