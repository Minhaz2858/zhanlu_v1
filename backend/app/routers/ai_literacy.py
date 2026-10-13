"""AI literacy router — starter prompts + prompt coach (IBM Gap 4).

Both endpoints serve EVERY authenticated user — literacy is a user
feature, not an admin one.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.services.ai_literacy import coach_question, starter_prompts

router = APIRouter(tags=["literacy"])


@router.get("/literacy/starter-prompts")
def get_starter_prompts(
    days: int = Query(default=90, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Suggested questions — your team's successful asks first."""
    return starter_prompts(db, org_id=user.org_id, days=days)


class CoachBody(BaseModel):
    question: str


@router.post("/literacy/prompt-coach")
async def post_prompt_coach(
    body: CoachBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Rewrite a rough question into sharper variants + one tip.

    Fail-open: always returns something usable, never blocks asking.
    """
    return await coach_question(body.question)
