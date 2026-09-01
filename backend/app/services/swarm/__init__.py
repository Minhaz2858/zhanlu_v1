"""Public surface of the swarm runtime.

This module re-exports the durable primitives from ``mailbox.py`` so the
existing call sites in ``tool_handlers/swarm_tools.py`` and
``tool_handlers/mixture_of_agents_tool.py`` keep working after the
scaffold-to-runtime migration.

Backward compatibility: the old ``Agent``, ``Team``, and ``Handoff``
classes were dataclass-only.  The new ``Handoff`` is a richer dataclass
that also serializes to JSON; callers that constructed the old dataclass
with positional args must update to keyword args.
"""

from app.services.swarm.mailbox import (
    Handoff,
    HandoffProtocol,
    Mailbox,
    RoleSpec,
    get_role,
    list_roles,
    register_role,
    spawn_subagent,
)
from app.services.swarm.team_registry import (
    Team,
    TeamRegistry,
    SwarmCoordinator,
    get_team_registry,
    get_swarm_coordinator,
)

__all__ = [
    "Handoff",
    "HandoffProtocol",
    "Mailbox",
    "RoleSpec",
    "get_role",
    "list_roles",
    "register_role",
    "spawn_subagent",
    "Team",
    "TeamRegistry",
    "SwarmCoordinator",
    "get_team_registry",
    "get_swarm_coordinator",
]
