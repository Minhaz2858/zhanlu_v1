"""Report endpoints: Customer Sales + Contract Performance.

Registered in main.py with prefix="/api".
Requires auth (`get_current_user_required`) and EDIA reachability (503 fail-closed).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.deps import get_current_user_required
from app.core.mysql_db import is_edia_reachable
from app.schemas.reports import (
    CustomerSalesReportResponse,
    ContractPerformanceResponse,
)
from app.services.report_service import (
    load_contract_execution,
    build_customer_sales_report,
    build_contract_performance_report,
)
from app.services.report_ai_service import stream_ai_analysis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


def _require_edia(request: Request = None):
    """Fail-closed: if EDIA MySQL can't be reached, return 503."""
    if not is_edia_reachable():
        raise HTTPException(
            status_code=503,
            detail="edia_unavailable",
        )


# ── Customer Sales ───────────────────────────────────────────────────────

@router.get(
    "/customer-sales",
    response_model=CustomerSalesReportResponse,
    summary="Customer sales report",
)
async def get_customer_sales(
    month: str = Query("", description="YYYY-MM month filter"),
    material: str = Query("", description="Material/product name filter"),
    org: str = Query("huizhou", description="Org: huizhou or guangdong"),
    debug: bool = Query(False, description="Include price-source diagnostics"),
    _: dict = Depends(get_current_user_required),
):
    """Returns customer-sales KPI cards, donut chart, weekly price/sales
    charts, sales detail table, and available filter options.
    """
    _require_edia()

    rows, org_meta = load_contract_execution(org)
    data = build_customer_sales_report(
        rows, org_meta, month=month, material=material, debug=debug,
    )
    return data


# ── Contract Performance ─────────────────────────────────────────────────

@router.get(
    "/contract-performance",
    response_model=ContractPerformanceResponse,
    summary="Sales contract execution performance report",
)
async def get_contract_performance(
    start_date: str = Query("", description="Start date YYYY-MM-DD"),
    end_date: str = Query("", description="End date YYYY-MM-DD"),
    material: str = Query("", description="Material/product name filter"),
    org: str = Query("huizhou", description="Org: huizhou or guangdong"),
    _: dict = Depends(get_current_user_required),
):
    """Returns execution KPIs, execution chart, unshipped-contracts table."""
    _require_edia()

    rows, org_meta = load_contract_execution(org)
    data = build_contract_performance_report(
        rows, org_meta,
        start_date=start_date, end_date=end_date, material=material,
    )

    return data


# ── AI Analysis (SSE streaming) ──────────────────────────────────────────

@router.get(
    "/contract-performance/ai-analysis",
    summary="AI analysis for contract performance (SSE stream)",
)
async def get_contract_performance_ai_analysis(
    start_date: str = Query("", description="Start date YYYY-MM-DD"),
    end_date: str = Query("", description="End date YYYY-MM-DD"),
    material: str = Query("", description="Material/product name filter"),
    org: str = Query("huizhou", description="Org: huizhou or guangdong"),
    stream: bool = Query(True, description="Always enable SSE streaming"),
    _: dict = Depends(get_current_user_required),
):
    """Returns AI analysis as SSE (Server-Sent Events) stream.

    Events: meta → (delta|partial)* → done
    Matches EDIA's protocol for incremental rendering in ContractPerformanceReport.
    """
    _require_edia()

    rows, org_meta = load_contract_execution(org)
    data = build_contract_performance_report(
        rows, org_meta,
        start_date=start_date, end_date=end_date, material=material,
    )

    return StreamingResponse(
        stream_ai_analysis(
            execution_chart=data.get("execution_chart", []),
            unshipped_contracts=data.get("unshipped_contracts", []),
            org_meta=org_meta,
            start_date=data["start_date"],
            end_date=data["end_date"],
            material=material,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
