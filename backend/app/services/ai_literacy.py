"""AI literacy service — in-product literacy features (IBM Gap 4).

IBM's AI-literacy research: transformations stall when users don't know
what to ask or how to evaluate answers; literacy must be taught "connected
to real tools and workflows", not abstractly. The most in-context moment
to teach is INSIDE the chat surface.

Two features, both grounded in the tenant's own data (the intelligence
moat — suggestions come from what YOUR team actually did, never generic):

  1. starter_prompts — "questions that worked for your team", mined from
     successful nl2sql turns (thumbs-up preferred), plus capability
     starters. Teaches ENGAGING with AI by example.
  2. coach_question — rewrites a rough question into sharper variants
     with a one-line tip (specificity, time window, output shape).
     Teaches CREATING with AI at the exact moment of asking.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

_GENERIC_STARTERS = [
    "What can you help me with?",
    "Show me a summary of the latest data in my connected sources",
    "Build a report from my data and export it as a document",
    "What trends do you see in my numbers this month?",
]

_COACH_PROMPT = """You are a prompt coach inside an enterprise AI assistant. The user \
typed a rough question. Rewrite it into 2-3 sharper versions that a data \
agent can answer better, and give ONE short tip (max 20 words) about \
what makes a good question here (specificity, time window, output shape \
— pick the most relevant advice).

Rough question: "{question}"

Reply with ONLY JSON:
{{"improved": ["...", "..."], "tip": "..."}}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def starter_prompts(db: Session, org_id: str, days: int = 90, limit: int = 6) -> dict:
    """Suggested questions: the tenant's own successful asks first.

    Teaches by example — new users see what this deployment's data can
    answer, phrased the way their colleagues already phrase it.
    """
    from datetime import datetime, timedelta, timezone

    from app.models.nl2sql_query_log import Nl2sqlQueryLog

    start = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days), 365)))
    rows = (
        db.query(
            Nl2sqlQueryLog.question,
            Nl2sqlQueryLog.user_feedback,
            Nl2sqlQueryLog.created_date,
        )
        .filter(
            Nl2sqlQueryLog.org_id == org_id,
            Nl2sqlQueryLog.is_deleted == False,  # noqa: E712
            Nl2sqlQueryLog.outcome == "success",
            Nl2sqlQueryLog.created_date >= start,
        )
        .all()
    )

    # Rank: thumbs-up first, then recency; dedupe case-insensitively.
    def _rank(row):
        _, feedback, created = row
        return (0 if feedback == "thumbs_up" else 1, -created.timestamp())

    seen: set[str] = set()
    worked: list[dict] = []
    for question, _fb, _created in sorted(rows, key=_rank):
        q = (question or "").strip()
        key = q.lower()
        if not q or len(q) < 8 or key in seen:
            continue
        seen.add(key)
        worked.append({"question": q[:200], "source": "your_team"})
        if len(worked) >= max(1, limit - 2):
            break

    starters = worked + [
        {"question": g, "source": "suggested"}
        for g in _GENERIC_STARTERS
    ]
    return {
        "starters": starters[:limit],
        "tip": (
            "Questions that name a measure, a time window, and a grouping "
            "(“revenue by product last month”) get the best answers."
            if worked else
            "Ask about the data in your connected sources — reports, "
            "trends, rankings, or documents to export."
        ),
        "worked_count": len(worked),
    }


async def coach_question(question: str, endpoint=None) -> dict:
    """Rewrite a rough question into sharper variants + one tip.

    Fail-open: on any error, returns the original with a generic tip —
    a literacy feature must never block asking.
    """
    from app.services.llm_service import call_llm

    q = (question or "").strip()
    if not q:
        return {"improved": [], "tip": "Describe what you want to see.", "original": ""}

    try:
        result = await call_llm(
            prompt=_COACH_PROMPT.format(question=q[:600]),
            temperature=0.3,
            task_type="prompt_coach",
            endpoint=endpoint,
            response_json_schema={
                "type": "object",
                "properties": {
                    "improved": {"type": "array", "items": {"type": "string"}},
                    "tip": {"type": "string"},
                },
                "required": ["improved", "tip"],
            },
        )
        parsed = result.get("data")
        if not isinstance(parsed, dict):
            text = str(result.get("response") or "")
            match = _JSON_RE.search(text)
            if not match:
                raise ValueError("no JSON")
            import json as _json

            parsed = _json.loads(match.group(0))
        improved = [
            str(v).strip()[:300]
            for v in (parsed.get("improved") or [])
            if str(v).strip()
        ][:3]
        tip = str(parsed.get("tip") or "").strip()[:200]
        if not improved:
            raise ValueError("empty variants")
        return {"improved": improved, "tip": tip, "original": q}
    except Exception as exc:  # noqa: BLE001 — literacy must never block asking
        logger.debug("prompt coach failed (fail-open): %s", exc)
        return {
            "improved": [],
            "tip": (
                "Good questions name a measure, a time window, and the "
                "grouping or output you want."
            ),
            "original": q,
        }
