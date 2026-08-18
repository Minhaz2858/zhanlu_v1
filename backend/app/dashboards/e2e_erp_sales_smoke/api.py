"""Generated dashboard app router for `e2e-erp-sales-smoke`. DO NOT EDIT.

Exposes:
- GET  /config            — the frontend config (metrics, design tokens, theme)
- GET  /metrics           — metric list (id/title/type)
- GET  /metrics/{id}      — live metric payload (columns/rows/error)
- WS   /ws                — WebSocket channel; the shared ConnectionManager
                            broadcasts rows when the poller detects a change

Mounted by ``DashboardAppManager`` as a sub-router on the main FastAPI app.
"""
import asyncio

from fastapi import APIRouter, Depends, WebSocket
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.dashboard_app.realtime import get_connection_manager

from .queries import METRICS, run_metric

SLUG = 'e2e-erp-sales-smoke'
CONFIG = {'name': 'E2E ERP Sales Smoke', 'slug': 'e2e-erp-sales-smoke', 'description': 'T3 E2E smoke dashboard', 'theme': 'light', 'refresh_interval_seconds': 30, 'design_system_ref': None, 'metrics': [{'id': 'kpi_total_qty', 'title': 'Total Shipment Qty', 'type': 'kpi', 'options': {'value_column': 'total_qty', 'format': 'integer'}}, {'id': 'line_monthly_trend', 'title': 'Monthly Shipment Trend', 'type': 'line', 'options': {'x_column': 'month', 'y_column': 'qty'}}, {'id': 'table_recent', 'title': 'Recent Shipments', 'type': 'table', 'options': {}}]}

router = APIRouter(prefix=f"/api/dashboards/apps/{SLUG}")


@router.get("/config")
async def get_config() -> dict:
    return CONFIG


@router.get("/metrics")
async def list_metrics() -> dict:
    return {"metrics": [
        {"id": m["id"], "title": m["title"], "type": m["type"]} for m in METRICS
    ]}


@router.get("/metrics/{metric_id}")
async def get_metric(metric_id: str, db: Session = Depends(get_db)) -> dict:
    return {"metric_id": metric_id, "data": await run_metric(db, metric_id)}


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    mgr = get_connection_manager()
    await mgr.connect(SLUG, websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / disconnect detect
    except Exception:
        pass
    finally:
        mgr.disconnect(SLUG, websocket)
