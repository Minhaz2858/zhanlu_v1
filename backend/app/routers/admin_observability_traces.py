"""Admin trace-metrics router (Phase 1: OTel end-to-end).

Serves the in-process GenAI metrics rollup so the Admin observability
dashboard can show LLM cost/latency, tool latency/errors, FSM state
distribution and workflow-node stats — without querying the OTLP collector
directly.

All endpoints require the ``admin`` role (enforced by ``require_admin``).
"""

import json
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import require_admin, get_db
from app.services.tracing.metrics import get_metrics, metrics_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/traces", tags=["admin-traces"])


@router.get("/metrics")
def get_trace_metrics(admin=Depends(require_admin)) -> dict:
    """Return the aggregated GenAI metrics snapshot for the dashboard."""
    return {
        "enabled": metrics_enabled(),
        "snapshot": get_metrics().snapshot(),
    }


@router.post("/metrics/reset")
def reset_trace_metrics(admin=Depends(require_admin)) -> dict:
    """Clear the in-process metrics rollup (e.g. after a demo/benchmark)."""
    get_metrics().reset()
    return {"success": True}


@router.get("/eval-results")
def get_eval_results(
    limit: int = 50,
    db: "Session" = Depends(get_db),
    admin=Depends(require_admin),
) -> dict:
    """Eval pipeline results (ADLC P0-2): latest LLM-as-judge verdicts +
    summary pass rate per dimension. Supports the Operate-phase dashboard."""
    from app.config import settings
    from app.models.eval_result import EvalResult

    rows = (
        db.query(EvalResult)
        .order_by(EvalResult.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    dims: dict[str, list[float]] = {}
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        try:
            scores = json.loads(r.scores or "{}")
        except (TypeError, ValueError):
            scores = {}
        for dim, score in scores.items():
            if isinstance(score, (int, float)):
                dims.setdefault(dim, []).append(float(score))

    return {
        "pipeline_enabled": getattr(settings, "EVAL_PIPELINE_ENABLED", False),
        "sample_rate": getattr(settings, "EVAL_SAMPLE_RATE", 0.1),
        "total_returned": len(rows),
        "verdicts": verdicts,
        "dimension_averages": {
            dim: round(sum(s) / len(s), 3) for dim, s in dims.items() if s
        },
        "results": [
            {
                "id": r.id,
                "conversation_id": r.conversation_id,
                "user_message": (r.user_message or "")[:200],
                "scores": _safe_scores(r.scores),
                "verdict": r.verdict,
                "created_date": r.created_date.isoformat() if r.created_date else None,
            }
            for r in rows
        ],
    }


def _safe_scores(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
