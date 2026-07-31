"""API router for OpenHarness-migrated services.

Exposes endpoints for:
- Agent definitions (list, get)
- Skills (list, search, get)
- Background tasks (create, get, list, stop, output)
- Token usage (total, records)
- Permissions (check)
- Provider profiles (list, get, set active)
- Swarm teams (create, list, get, add member, send message)
- ohmo workspace (get/set soul, identity, user, memories)
"""

from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openharness"], dependencies=[Depends(get_current_user_required)])


# ---------------------------------------------------------------------------
# Agent Definitions
# ---------------------------------------------------------------------------

@router.get("/agent-definitions")
async def list_agent_definitions():
    """List all available agent definitions."""
    from app.services.agent_definitions import list_agent_definitions
    agents = list_agent_definitions()
    return [a.model_dump() for a in agents]


@router.get("/agent-definitions/{name}")
async def get_agent_definition(name: str):
    """Get a specific agent definition."""
    from app.services.agent_definitions import get_agent_definition
    agent = get_agent_definition(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent definition '{name}' not found")
    return agent.model_dump()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@router.get("/skills")
async def list_skills(category: str | None = Query(None)):
    """List all loaded skills."""
    from app.services.skills_loader import list_skills
    skills = list_skills(category)
    return [s.to_dict() for s in skills]


@router.get("/skills/search")
async def search_skills(q: str = Query(...), limit: int = Query(10)):
    """Search skills by query."""
    from app.services.skills_loader import search_skills
    results = search_skills(q, limit=limit)
    return [s.to_dict() for s in results]


@router.get("/skills/unified-search")
async def unified_search_skills(
    q: str = Query(...),
    limit: int = Query(10),
    db: Session = Depends(get_db),
):
    """Search skills across both DB tools table and filesystem marketplace."""
    from app.services.skills_loader import unified_search
    return unified_search(q, limit=limit, db=db)


@router.get("/skills/categories")
async def list_skill_categories():
    """List all skill categories."""
    from app.services.skills_loader import get_skills_registry
    return get_skills_registry().list_categories()


@router.get("/skills/{name}")
async def get_skill(name: str):
    """Get a specific skill."""
    from app.services.skills_loader import get_skill
    skill = get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return skill.to_dict()


# ---------------------------------------------------------------------------
# Background Tasks
# ---------------------------------------------------------------------------

@router.post("/tasks")
async def create_task(body: dict):
    """Create a background task."""
    from app.services.background_tasks import get_task_manager, TaskType
    tm = get_task_manager()
    task = tm.create(
        name=body.get("name", "Untitled Task"),
        type=body.get("type", TaskType.SHELL.value),
        command=body.get("command"),
        agent_name=body.get("agent_name"),
        metadata=body.get("metadata"),
    )
    return task.to_dict()


@router.get("/tasks")
async def list_tasks(status: str | None = Query(None)):
    """List background tasks."""
    from app.services.background_tasks import get_task_manager
    tm = get_task_manager()
    tasks = tm.list_tasks(status)
    return [t.to_dict() for t in tasks]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Get a background task by ID."""
    from app.services.background_tasks import get_task_manager
    tm = get_task_manager()
    task = tm.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


@router.delete("/tasks/{task_id}")
async def stop_task(task_id: str):
    """Stop a running background task."""
    from app.services.background_tasks import get_task_manager
    tm = get_task_manager()
    success = tm.stop(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Task not found or not running")
    return {"success": True, "message": "Task stopped"}


# ---------------------------------------------------------------------------
# Token Usage
# ---------------------------------------------------------------------------

@router.get("/token-usage")
async def get_token_usage(conversation_id: str | None = Query(None)):
    """Get token usage statistics."""
    from app.services.token_tracker import get_token_tracker
    tt = get_token_tracker()
    return tt.get_total_usage(conversation_id)


@router.get("/token-usage/records")
async def get_token_records(
    conversation_id: str | None = Query(None),
    limit: int = Query(100),
):
    """Get token usage records."""
    from app.services.token_tracker import get_token_tracker
    tt = get_token_tracker()
    records = tt.get_records(conversation_id, limit=limit)
    return [r.to_dict() for r in records]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@router.post("/permissions/check")
async def check_permission(body: dict):
    """Check if a tool call is allowed."""
    from app.services.permissions import check_permission
    result = check_permission(
        tool_name=body.get("tool_name", ""),
        args=body.get("args", {}),
        agent_name=body.get("agent_name"),
    )
    return {
        "allowed": result.allowed,
        "reason": result.reason,
        "requires_confirmation": result.requires_confirmation,
    }


# ---------------------------------------------------------------------------
# Provider Profiles
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """List all configured provider profiles."""
    from app.services.providers import get_provider_manager
    pm = get_provider_manager()
    return [p.to_dict() for p in pm.list_profiles()]


@router.get("/providers/{name}")
async def get_provider(name: str):
    """Get a specific provider profile."""
    from app.services.providers import get_provider_manager
    pm = get_provider_manager()
    profile = pm.get_profile(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return profile.to_dict()


@router.post("/providers/{name}/activate")
async def activate_provider(name: str):
    """Set the active provider profile."""
    from app.services.providers import get_provider_manager
    pm = get_provider_manager()
    success = pm.set_active(name)
    if not success:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return {"success": True, "active_provider": name}


# ---------------------------------------------------------------------------
# Swarm Teams
# ---------------------------------------------------------------------------

@router.post("/teams")
async def create_team(body: dict):
    """Create a new agent team."""
    from app.services.swarm import get_team_registry
    tr = get_team_registry()
    team = tr.create_team(
        name=body.get("name", "Untitled Team"),
        description=body.get("description", ""),
    )
    return team.to_dict()


@router.get("/teams")
async def list_teams():
    """List all teams."""
    from app.services.swarm import get_team_registry
    tr = get_team_registry()
    return [t.to_dict() for t in tr.list_teams()]


@router.get("/teams/{team_id}")
async def get_team(team_id: str):
    """Get a team by ID."""
    from app.services.swarm import get_team_registry
    tr = get_team_registry()
    team = tr.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.to_dict()


@router.post("/teams/{team_id}/members")
async def add_team_member(team_id: str, body: dict):
    """Add a member to a team."""
    from app.services.swarm import get_team_registry
    tr = get_team_registry()
    success = tr.add_member(
        team_id,
        name=body.get("name", ""),
        agent_name=body.get("agent_name", ""),
        role=body.get("role", "member"),
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to add member")
    return {"success": True}


@router.post("/teams/{team_id}/messages")
async def send_team_message(team_id: str, body: dict):
    """Send a message between team members."""
    from app.services.swarm import get_team_registry
    tr = get_team_registry()
    success = tr.send_message(
        team_id,
        sender=body.get("sender", ""),
        recipient=body.get("recipient", "main"),
        content=body.get("content", ""),
        summary=body.get("summary", ""),
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to send message")
    return {"success": True}


# ---------------------------------------------------------------------------
# ohmo Workspace
# ---------------------------------------------------------------------------

@router.get("/ohmo/soul")
async def get_ohmo_soul():
    """Get the ohmo agent soul definition."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    ws.init_workspace()
    return {"soul": ws.get_soul()}


@router.put("/ohmo/soul")
async def set_ohmo_soul(body: dict):
    """Update the ohmo agent soul definition."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    ws.set_soul(body.get("soul", ""))
    return {"success": True}


@router.get("/ohmo/user")
async def get_ohmo_user():
    """Get the ohmo user profile."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    return {"user": ws.get_user_profile()}


@router.put("/ohmo/user")
async def set_ohmo_user(body: dict):
    """Update the ohmo user profile."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    ws.set_user_profile(body.get("user", ""))
    return {"success": True}


@router.get("/ohmo/memories")
async def list_ohmo_memories():
    """List ohmo memories."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    return {"memories": ws.list_memories()}


@router.get("/ohmo/memories/{name}")
async def get_ohmo_memory(name: str):
    """Get a specific ohmo memory."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    content = ws.get_memory(name)
    if content is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"name": name, "content": content}


@router.put("/ohmo/memories/{name}")
async def set_ohmo_memory(name: str, body: dict):
    """Update or create an ohmo memory."""
    from app.services.ohmo import get_ohmo_workspace
    ws = get_ohmo_workspace()
    path = ws.save_memory(name, body.get("content", ""))
    return {"success": True, "path": path}
