"""Agent Studio router — manifest, bindings, preflight, and dry-run APIs.

Provides:
* GET /api/agent-studio/{agent_id}/preflight — run preflight checks
* POST /api/agent-studio/{agent_id}/dry-run — dry-run with test message
* GET /api/agent-studio/{agent_id}/bindings — list data + skill bindings
* POST /api/agent-studio/{agent_id}/bindings/data — add data binding
* POST /api/agent-studio/{agent_id}/bindings/skill — add skill binding

Security: every endpoint requires an authenticated user.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-studio"])


class DataBindingRequest(BaseModel):
    datasource_id: str
    access_mode: str = "read_only"
    allowed_tables: Optional[list] = None
    allowed_columns: Optional[dict] = None
    nl2sql_enabled: bool = True


class SkillBindingRequest(BaseModel):
    skill_name: str
    skill_version: Optional[str] = None
    is_allowed: bool = True
    is_pinned: bool = False


@router.get("/agent-studio/{agent_id}/preflight")
def run_preflight(
    agent_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Run preflight readiness checks on an agent."""
    from app.services.agent_studio.preflight import PreflightService
    service = PreflightService(db)
    return service.check_agent(agent_id)


@router.post("/agent-studio/{agent_id}/dry-run")
def dry_run(
    agent_id: str,
    test_message: str = "Hello, what can you do?",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Perform a dry-run of the agent."""
    from app.services.agent_studio.preflight import PreflightService
    service = PreflightService(db)
    return service.dry_run(agent_id, test_message)


@router.get("/agent-studio/{agent_id}/bindings")
def get_bindings(
    agent_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List data and skill bindings for an agent."""
    from app.models.agent_data_binding import AgentDataBinding
    from app.models.agent_skill_binding import AgentSkillBinding

    data_bindings = (
        db.query(AgentDataBinding)
        .filter(AgentDataBinding.agent_app_id == agent_id, AgentDataBinding.is_deleted == False)
        .all()
    )
    skill_bindings = (
        db.query(AgentSkillBinding)
        .filter(AgentSkillBinding.agent_app_id == agent_id, AgentSkillBinding.is_deleted == False)
        .all()
    )

    return {
        "data_bindings": [b.to_dict() for b in data_bindings],
        "skill_bindings": [b.to_dict() for b in skill_bindings],
    }


@router.post("/agent-studio/{agent_id}/bindings/data")
def add_data_binding(
    agent_id: str,
    req: DataBindingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add a data binding to an agent."""
    from app.models.agent_data_binding import AgentDataBinding
    from uuid import uuid4

    binding = AgentDataBinding(
        id=str(uuid4()),
        agent_app_id=agent_id,
        datasource_id=req.datasource_id,
        access_mode=req.access_mode,
        allowed_tables=req.allowed_tables,
        allowed_columns=req.allowed_columns,
        nl2sql_enabled=req.nl2sql_enabled,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding.to_dict()


@router.post("/agent-studio/{agent_id}/bindings/skill")
def add_skill_binding(
    agent_id: str,
    req: SkillBindingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add a skill binding to an agent."""
    from app.models.agent_skill_binding import AgentSkillBinding
    from uuid import uuid4

    binding = AgentSkillBinding(
        id=str(uuid4()),
        agent_app_id=agent_id,
        skill_name=req.skill_name,
        skill_version=req.skill_version,
        is_allowed=req.is_allowed,
        is_pinned=req.is_pinned,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding.to_dict()
