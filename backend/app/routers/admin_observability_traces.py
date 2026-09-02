"""Admin trace-metrics router (Phase 1: OTel end-to-end).

Serves the in-process GenAI metrics rollup so the Admin observability
dashboard can show LLM cost/latency, tool latency/errors, FSM state
distribution and workflow-node stats — without querying the OTLP collector
directly.

All endpoints require the ``admin`` role (enforced by ``require_admin``).
"""

import logging

from fastapi import APIRouter, Depends

from app.deps import require_admin
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
