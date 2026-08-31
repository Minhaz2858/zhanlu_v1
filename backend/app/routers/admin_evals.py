"""Admin eval/regression endpoints (Phase 0 agent-gaps build, 2026-08-29).

Read-only surface over the eval loop:
- GET /api/admin/evals            — latest eval_results rows
- GET /api/admin/evals/summary    — pass rate by dimension + per-model breakdown
- GET /api/admin/evals/cases      — golden test cases + run history

All endpoints require the ``admin`` role (``require_admin``), mirroring
admin_users.py.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.config import settings
from app.models.agent_test_case import AgentTestCase
from app.models.eval_result import EvalResult
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/evals", tags=["admin-evals"])

_ACCEPT_VERDICTS = {"accept", "pass", "passing"}


def _parse_scores(scores_text: str | None) -> dict:
    if not scores_text:
        return {}
    try:
        data = json.loads(scores_text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — corrupt row must not break the endpoint
        return {}


@router.get("")
def list_evals(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    rows = (
        db.query(EvalResult)
        .order_by(EvalResult.created_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "conversation_id": r.conversation_id,
                "verdict": r.verdict,
                "model": r.model,
                "scores": _parse_scores(r.scores),
                "assistant_excerpt": (r.assistant_text or "")[:200],
                "created_date": r.created_date.isoformat() if r.created_date else None,
            }
            for r in rows
        ],
    }


@router.get("/summary")
def eval_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    rows = db.query(EvalResult).all()
    total = len(rows)
    if total == 0:
        return {"total": 0, "pass_rate": 0.0, "dimensions": {}, "by_model": {}, "by_verdict": {}}

    by_verdict: dict[str, int] = defaultdict(int)
    dim_scores: dict[str, list[float]] = defaultdict(list)
    model_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "pass": 0, "sum_scores": 0.0, "dim_count": 0})

    for r in rows:
        by_verdict[r.verdict or "unknown"] += 1
        scores = _parse_scores(r.scores)
        for dim, score in scores.items():
            try:
                dim_scores[dim].append(float(score))
            except (TypeError, ValueError):
                continue
        stats = model_stats[r.model or "unknown"]
        stats["count"] += 1
        if r.verdict in _ACCEPT_VERDICTS:
            stats["pass"] += 1
        for score in scores.values():
            try:
                stats["sum_scores"] += float(score)
                stats["dim_count"] += 1
            except (TypeError, ValueError):
                continue

    accept_count = sum(v for k, v in by_verdict.items() if k in _ACCEPT_VERDICTS)
    return {
        "total": total,
        "pass_rate": round(accept_count / max(total, 1), 3),
        "dimensions": {
            dim: round(sum(scores) / len(scores), 3) for dim, scores in dim_scores.items()
        },
        "by_verdict": dict(by_verdict),
        "by_model": {
            model: {
                "count": stats["count"],
                "pass": stats["pass"],
                "pass_rate": round(stats["pass"] / max(stats["count"], 1), 3),
                "avg_score": round(stats["sum_scores"] / max(stats["dim_count"], 1), 3)
                if stats["dim_count"] else None,
            }
            for model, stats in sorted(model_stats.items())
        },
    }


@router.get("/cases")
def list_cases(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    rows = (
        db.query(AgentTestCase)
        .order_by(AgentTestCase.name.asc())
        .all()
    )
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "test_type": r.test_type,
                "status": r.status,
                "run_count": r.run_count,
                "pass_count": r.pass_count,
                "last_result": r.last_result,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "input": r.input_json,
            }
            for r in rows
        ],
    }


# ── Golden-eval regression gate (preflight) ────────────────────────────────
# POST /api/admin/evals/regression — run the golden suite against a CANDIDATE
# model (saved row by model_id, or an unsaved dialog candidate) and return a
# pass/warn/fail report vs the champion. The UI calls this BEFORE submitting
# the model change; the inline gate in llm.py enforces it when
# EVAL_GATE_ENABLED=true. Works regardless of the gate flag.


def _current_champion_label(db: Session) -> str:
    """The champion model label = the catalog default, else the env model."""
    try:
        from app.models.llm_model import LlmModel

        row = (
            db.query(LlmModel)
            .filter(
                LlmModel.is_default == True,  # noqa: E712
                LlmModel.enabled == True,  # noqa: E712
                LlmModel.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if row is not None:
            return row.model_id
    except Exception:  # noqa: BLE001
        pass
    return getattr(settings, "LLM_MODEL", None) or ""


def _build_candidate_endpoint(db: Session, body: dict):
    """Build a candidate LLMEndpoint from a saved model_id or raw fields."""
    from app.models.llm_model import LlmModel
    from app.services.crypto_utils import decrypt_value
    from app.services.llm_router import LLMEndpoint

    model_id = body.get("model_id")
    if not model_id:
        raise HTTPException(status_code=422, detail="model_id is required for the regression gate")

    # Saved row path: look up by row id OR by model_id.
    row = None
    if body.get("id"):
        row = db.query(LlmModel).filter(LlmModel.id == body["id"]).first()
    if row is None:
        row = (
            db.query(LlmModel)
            .filter(LlmModel.model_id == model_id)
            .order_by(LlmModel.created_date.desc())
            .first()
        )

    if row is not None:
        return LLMEndpoint(
            base_url=row.base_url,
            api_key=decrypt_value(row.api_key) if row.api_key else "",
            model_id=row.model_id,
            is_private=bool(row.is_private),
            bypass_hallucination_guardrail=bool(row.bypass_hallucination_guardrail),
            provider=row.provider or "",
            context_window=row.context_window,
            max_output_tokens=row.max_output_tokens,
            supports_structured_tool_calls=bool(row.supports_structured_tool_calls),
        )

    # Unsaved candidate path (admin dialog not yet persisted).
    base_url = body.get("base_url")
    api_key = body.get("api_key")
    if not base_url or not api_key:
        raise HTTPException(
            status_code=422,
            detail="Unknown model_id — provide base_url + api_key for an unsaved candidate",
        )
    return LLMEndpoint(
        base_url=base_url,
        api_key=api_key,
        model_id=model_id,
        provider=body.get("provider", ""),
        context_window=body.get("context_window"),
        max_output_tokens=body.get("max_output_tokens"),
    )


@router.post("/regression")
async def run_regression(
    body: dict,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Run the golden suite against a candidate model and return the gate report.

    Body: {"model_id": "...", "id"?: "...", "base_url"?: "...", "api_key"?:
    "...", "provider"?: "...", "context_window"?: ..., "max_output_tokens"?:
    ...}. A saved row is used when found; otherwise base_url+api_key must be
    supplied for an unsaved candidate.
    """
    from app.services.golden_eval_runner import run_golden_suite

    endpoint = _build_candidate_endpoint(db, body)
    champion_label = _current_champion_label(db)

    try:
        report = await run_golden_suite(
            db,
            endpoint=endpoint,
            model_label=endpoint.model_id,
            champion_label=champion_label,
            user_id=getattr(_, "id", None) or "golden-runner",
        )
    except Exception as exc:  # noqa: BLE001 — gate must never 500 the admin UI
        logger.exception("golden regression run failed")
        return {
            "status": "fail",
            "error": f"golden regression run failed: {exc}",
            "candidate": {"model": body.get("model_id"), "n": 0, "pass_rate": 0.0, "mean_completeness": 0.0},
            "champion": None,
            "cases": [],
            "regressed_cases": [],
        }
    return report
