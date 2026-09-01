"""Generated dashboard app router for `ceo-demo-001`. DO NOT EDIT.

Exposes:
- GET  /config            — the frontend config (metrics, design tokens, theme)
- GET  /metrics           — metric list (id/title/type)
- GET  /metrics/{id}      — live metric payload (columns/rows/error)
- WS   /ws                — WebSocket channel; the shared ConnectionManager
                            broadcasts rows when the poller detects a change

Mounted by ``DashboardAppManager`` as a sub-router on the main FastAPI app.
"""
import asyncio

from fastapi import APIRouter, Depends, Request, WebSocket
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.auth_service import auth_service
from app.services.dashboard_app.realtime import get_connection_manager

from .queries import METRICS, run_metric

SLUG = 'ceo-demo-001'
CONFIG = {'name': 'Ecisco CEO Decision Center', 'slug': 'ceo-demo-001', 'description': 'Executive decision center over the demo sales dataset (CEO style).', 'theme': 'dark', 'style': 'ceo', 'refresh_interval_seconds': 30, 'design_system_ref': None, 'design': {}, 'metrics': [{'id': 'kpi_revenue', 'title': 'Total Revenue', 'type': 'kpi', 'options': {}}, {'id': 'kpi_products', 'title': 'Products', 'type': 'kpi', 'options': {}}, {'id': 'kpi_orders', 'title': 'Orders', 'type': 'kpi', 'options': {}}, {'id': 'kpi_margin', 'title': 'Top Customer', 'type': 'kpi', 'options': {}}, {'id': 'bar_customers', 'title': 'Top 5 Customers by Revenue', 'type': 'bar', 'options': {'topItem': {'label': 'Acme Corp', 'value': 1250000.0, 'share_pct': 27.3}}}, {'id': 'bar_products', 'title': 'Top 5 Products by Volume', 'type': 'bar', 'options': {'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 26.6}}}, {'id': 'line_margin', 'title': 'Margin by Region', 'type': 'line', 'options': {'delta': 28.89, 'deltaLabel': 'vs prev. period', '_locked': {'current': 0.58, 'previous': 0.45}}}, {'id': 'table_orders', 'title': 'Largest Orders', 'type': 'table', 'options': {'topItem': {'label': 'Acme Corp', 'value': 120.0, 'share_pct': 21.1}}}], 'filters': [], 'insights': [{'title': 'Revenue Concentration', 'body': 'Acme Corp alone accounts for 27% of top-5 revenue.'}, {'title': 'Volume Leaders', 'body': 'Widget A + Widget B drive ~50% of top-5 volume.'}, {'title': '🏆 Top: Acme Corp', 'body': 'Acme Corp leads with 1,250,000 (27.3% of the total).'}, {'title': '🏆 Top: Widget A', 'body': 'Widget A leads with 170 (26.6% of the total).'}, {'title': '📈 Margin by Region', 'body': 'Margin by Region moved 28.9% up vs the previous period (locked server-side from the live datasource at build time).'}], 'layout': [{'title': 'Executive Pulse', 'widgets': ['kpi_revenue', 'kpi_products', 'kpi_orders', 'kpi_margin']}, {'title': 'Top Performers', 'widgets': ['bar_customers', 'bar_products']}, {'title': 'Margin by Region', 'widgets': ['line_margin']}, {'title': 'Order Detail', 'widgets': ['table_orders']}]}

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
async def get_metric(metric_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    # Deep-linked filtered views: every query-string key/value on the URL
    # (e.g. /metrics/sales?product=%E4%B9%99%E4%BA%8C%E9%86%87) is forwarded to
    # run_metric, which only substitutes DECLARED :dim_* tokens and SQL-escapes
    # every value. Undeclared keys are inert — see queries.metric_dimensions.
    filters = dict(request.query_params)
    return {"metric_id": metric_id, "data": await run_metric(db, metric_id, filters)}


def _authorize_ws_token(token: str | None) -> bool:
    """T9: only valid session access tokens may open the live WebSocket."""
    return bool(token) and bool(auth_service.verify_token(token))


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str | None = None) -> None:
    # T9: WebSocket auth — the client sends the session access token as a
    # query param (?token=…) because browser WebSockets cannot set headers.
    # Anonymous or invalid connections are rejected with close code 1008.
    if not _authorize_ws_token(token):
        await websocket.close(code=1008)
        return
    mgr = get_connection_manager()
    await mgr.connect(SLUG, websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / disconnect detect
    except Exception:
        pass
    finally:
        mgr.disconnect(SLUG, websocket)
