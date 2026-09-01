"""Skill Studio router — skill factory, review queue, and skill profile APIs.

Provides:
* POST /api/skill-studio/factory/create — create a skill candidate from description
* GET /api/skill-studio/candidates — list candidates in the review queue
* GET /api/skill-studio/candidates/{id} — get candidate detail
* POST /api/skill-studio/candidates/{id}/submit — submit for review
* POST /api/skill-studio/candidates/{id}/approve — approve candidate
* POST /api/skill-studio/candidates/{id}/reject — reject candidate
* GET /api/skill-studio/profiles — list published skill profiles
* GET /api/skill-studio/profiles/{id} — get skill profile detail
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skill-studio"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CreateFromDescriptionRequest(BaseModel):
    name: str
    description: str
    artifact_type: Optional[str] = None


class CreateFromCodeRequest(BaseModel):
    name: str
    code: str
    description: Optional[str] = None


class ReviewActionRequest(BaseModel):
    reviewed_by: str
    notes: Optional[str] = None


class RejectRequest(BaseModel):
    reviewed_by: str
    reason: str


class CollectFromUrlRequest(BaseModel):
    url: str
    skill_name: Optional[str] = None


# --- Skill Factory ---

@router.post("/skill-studio/factory/create")
async def create_from_description(req: CreateFromDescriptionRequest, db: Session = Depends(get_db)):
    """Create a skill candidate from a natural language description.

    The LLM generates a SKILL.md methodology document, which is persisted
    to the filesystem and the SkillsRegistry is reloaded so the skill is
    immediately available to all runtime agents.
    """
    from app.services.agent_studio.skill_factory import SkillFactory
    factory = SkillFactory(db)
    candidate = await factory.create_from_description(
        name=req.name,
        description=req.description,
        artifact_type=req.artifact_type,
    )
    return candidate.to_dict()


@router.post("/skill-studio/factory/create-from-code")
async def create_from_code(req: CreateFromCodeRequest, db: Session = Depends(get_db)):
    """Create a skill candidate from raw code."""
    from app.services.agent_studio.skill_factory import SkillFactory
    factory = SkillFactory(db)
    candidate = await factory.create_from_code(
        name=req.name,
        code=req.code,
        description=req.description,
    )
    return candidate.to_dict()


# --- Skill Collection (web scraping via agent-browser) ---

@router.post("/skills/collect")
async def collect_skill_from_url(req: CollectFromUrlRequest):
    """Collect a skill from a web URL using agent-browser.

    Drives the agent-browser CLI to navigate and extract page content,
    then uses the LLM to structure it as a SKILL.md methodology document.
    The skill is validated, persisted, and immediately available to all
    runtime agents.
    """
    from app.services.skill_collection_service import SkillCollectionService
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        service = SkillCollectionService(db=db)
        result = await service.collect_from_url(
            url=req.url,
            skill_name=req.skill_name,
        )
        if not result.get("success"):
            raise HTTPException(status_code=422, detail=result.get("error", "Collection failed"))
        return result
    finally:
        db.close()


# --- Skill Execution Tracking ---

@router.get("/skills/executions")
def list_skill_executions(
    skill_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List skill execution records (SkillRun) with optional filters.

    Returns execution evidence: skill name, agent, conversation, status,
    duration, and timestamp for every skill load/execute/run invocation
    recorded by the SkillExecutionRecorder.
    """
    from app.models.skill_run import SkillRun

    q = db.query(SkillRun).filter(SkillRun.is_deleted == False)

    # Filter by skill name (stored in input_json["skill_name"])
    if skill_name:
        q = q.filter(SkillRun.input_json["skill_name"].as_string() == skill_name)

    if status:
        q = q.filter(SkillRun.status == status)

    total = q.count()
    runs = (
        q.order_by(SkillRun.created_date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "success": True,
        "total": total,
        "count": len(runs),
        "offset": offset,
        "limit": limit,
        "executions": [_skill_run_to_dict(r) for r in runs],
    }


@router.post("/skills/{skill_name}/dry-run")
def trigger_dry_run(skill_name: str, db: Session = Depends(get_db)):
    """Trigger a dry-run validation gate for a skill.

    Auto-generates (or updates) a SkillTestCase and runs schema validation
    checks: non-empty body, required sections, registry discoverability,
    and security scan. Non-blocking — failures are warnings, not errors.
    """
    from app.services.skill_dry_run import run_dry_run_gate

    result = run_dry_run_gate(skill_name, db)
    return result


@router.get("/skills/{skill_name}/executions")
def list_skill_executions_by_name(
    skill_name: str,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List execution records for a specific skill."""
    from app.models.skill_run import SkillRun

    q = db.query(SkillRun).filter(
        SkillRun.is_deleted == False,
        SkillRun.input_json["skill_name"].as_string() == skill_name,
    )

    total = q.count()
    runs = (
        q.order_by(SkillRun.created_date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "success": True,
        "skill_name": skill_name,
        "total": total,
        "count": len(runs),
        "executions": [_skill_run_to_dict(r) for r in runs],
    }


def _skill_run_to_dict(run) -> dict:
    """Serialize a SkillRun model instance to a dict for API responses."""
    input_data = run.input_json or {}
    output_data = run.output_json or {}
    return {
        "id": run.id,
        "skill_name": input_data.get("skill_name", "unknown"),
        "skill_id": input_data.get("skill_id") or output_data.get("skill_id"),
        "skill_version": input_data.get("skill_version") or output_data.get("skill_version"),
        "lookup_name": input_data.get("lookup_name"),
        "action": input_data.get("action", "unknown"),
        "agent_name": input_data.get("agent_name", "unknown"),
        "conversation_id": run.conversation_id,
        "status": run.status,
        "duration_ms": run.duration_ms,
        "body_length": output_data.get("body_length"),
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_date": run.created_date.isoformat() if run.created_date else None,
    }


# --- Review Queue ---

@router.get("/skill-studio/candidates")
def list_candidates(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """List skill candidates in the review queue."""
    from app.services.agent_studio.review_queue import ReviewQueue
    queue = ReviewQueue(db)
    candidates = queue.list_candidates(status=status, limit=limit)
    return [c.to_dict() for c in candidates]


@router.get("/skill-studio/candidates/{candidate_id}")
def get_candidate(candidate_id: str, db: Session = Depends(get_db)):
    """Get skill candidate detail."""
    from app.services.agent_studio.review_queue import ReviewQueue
    queue = ReviewQueue(db)
    candidate = queue.get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate.to_dict()


@router.post("/skill-studio/candidates/{candidate_id}/submit")
def submit_for_review(candidate_id: str, req: ReviewActionRequest, db: Session = Depends(get_db)):
    """Submit a candidate for review."""
    from app.services.agent_studio.review_queue import ReviewQueue
    queue = ReviewQueue(db)
    candidate = queue.submit_for_review(candidate_id, reviewed_by=req.reviewed_by)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate.to_dict()


@router.post("/skill-studio/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, req: ReviewActionRequest, db: Session = Depends(get_db)):
    """Approve a candidate — creates a SkillProfile."""
    from app.services.agent_studio.review_queue import ReviewQueue
    queue = ReviewQueue(db)
    candidate = queue.approve(candidate_id, reviewed_by=req.reviewed_by, notes=req.notes)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate.to_dict()


@router.post("/skill-studio/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, req: RejectRequest, db: Session = Depends(get_db)):
    """Reject a candidate."""
    from app.services.agent_studio.review_queue import ReviewQueue
    queue = ReviewQueue(db)
    candidate = queue.reject(candidate_id, reviewed_by=req.reviewed_by, reason=req.reason)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate.to_dict()


# --- Skill Profiles ---

@router.get("/skill-studio/profiles")
def list_profiles(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """List published skill profiles."""
    from app.models.skill_profile import SkillProfile
    profiles = (
        db.query(SkillProfile)
        .filter(SkillProfile.is_deleted == False)
        .order_by(SkillProfile.created_date.desc())
        .limit(limit)
        .all()
    )
    return [p.to_dict() for p in profiles]


@router.get("/skill-studio/profiles/{profile_id}")
def get_profile(profile_id: str, db: Session = Depends(get_db)):
    """Get skill profile detail."""
    from app.models.skill_profile import SkillProfile
    profile = db.query(SkillProfile).filter(SkillProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile.to_dict()


# ── Default Skills API ──────────────────────────────────────────────────
#
# Returns the list of built-in default artifact-format skills that are
# always available to every agent. The frontend uses this to populate the
# "Default skills" section in the PlusMenu and to build default skill
# context for injection into the system prompt.


@router.get("/tools/defaults")
def list_default_skills_api():
    """List the built-in default skills always available to every agent."""
    try:
        from app.services.synexia.default_skills import get_default_skills_list
        skills_list = get_default_skills_list()
        return {"success": True, "default_skills": skills_list, "count": len(skills_list)}
    except Exception as exc:
        logger.warning("Failed to list default skills: %s", exc)
        return {"success": False, "default_skills": [], "count": 0, "error": str(exc)}


# ── Smart Skill Agent: draft state endpoints ─────────────────────────────
#
# The creation orchestrator keeps an in-flight ``SkillDraft`` per conversation.
# These endpoints let the frontend (a) reload the live folder tree after a page
# refresh, (b) save back edits from the inline markdown editor, and (c) discard
# a draft the user no longer wants. The orchestrator itself persists the draft
# through the module-level ``draft_store`` singleton, so these endpoints are
# thin read/write wrappers over that same store.


class DraftFileUpdateRequest(BaseModel):
    path: str          # "SKILL.md" or "references/<filename>"
    content: str


@router.get("/skill-studio/drafts/{conversation_id}")
def get_skill_draft(conversation_id: str):
    """Return the active SkillDraft for a conversation (live folder tree)."""
    from app.services.skill_studio.draft_store import draft_store

    draft = draft_store.get(conversation_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="No active skill draft")
    return {"draft": draft.to_dict()}


@router.put("/skill-studio/drafts/{conversation_id}/file")
def update_skill_draft_file(conversation_id: str, req: DraftFileUpdateRequest):
    """Update a single file (SKILL.md or references/*.md) in an active draft.

    This is the save-back endpoint for the inline markdown editor. It mutates
    the draft in place and re-persists it, so the live folder tree and the
    orchestrator (if it resumes later) both see the edited content.
    """
    from app.services.skill_studio.draft_store import draft_store

    draft = draft_store.get(conversation_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="No active skill draft")

    path = (req.path or "").strip()
    if path == "SKILL.md":
        draft.skill_md = req.content
    elif path.startswith("references/"):
        filename = path[len("references/"):]
        if not filename:
            raise HTTPException(status_code=400, detail="Invalid reference path")
        draft.references[filename] = req.content
    else:
        raise HTTPException(status_code=400, detail="Unsupported file path")

    draft_store.put(draft)
    return {"draft": draft.to_dict()}


@router.delete("/skill-studio/drafts/{conversation_id}")
def discard_skill_draft(conversation_id: str):
    """Discard the active SkillDraft for a conversation."""
    from app.services.skill_studio.draft_store import draft_store

    draft_store.delete(conversation_id)
    return {"success": True}
