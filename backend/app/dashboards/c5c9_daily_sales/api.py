"""Generated dashboard app router for `c5c9-daily-sales`. DO NOT EDIT.

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

SLUG = 'c5c9-daily-sales'
CONFIG = {'name': 'C5/C9 Daily Sales Dashboard', 'slug': 'c5c9-daily-sales', 'description': 'Real-time daily sales view for C5/C9: order volume, order value, product and customer breakdowns from the ERP warehouse.', 'theme': 'dark', 'style': 'ceo', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#8B5CF6', 'on_primary': '#FFFFFF', 'secondary': '#A78BFA', 'accent': '#FBBF24', 'background': '#0F0F23', 'foreground': '#F8FAFC', 'muted': '#27273B', 'border': '#4C1D95', 'destructive': '#EF4444', 'ring': '#8B5CF6', 'chart_palette': ['#8B5CF6', '#FBBF24', '#A78BFA', '#EF4444', '#FFFFFF', '#2563eb'], 'dark': {'background': '#0F0F23', 'foreground': '#F8FAFC', 'muted': '#27273B', 'border': '#4C1D95'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Sales Intelligence Dashboard', 'keywords': 'Deal pipeline, sales metrics, territory performance, sales rep leaderboard, win-loss analysis, quota tracking, forecast accuracy', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_value', 'title': 'Order Value (Last 30d)', 'type': 'kpi', 'options': {}}, {'id': 'kpi_volume', 'title': 'Order Volume (t)', 'type': 'kpi', 'options': {}}, {'id': 'kpi_lines', 'title': 'Order Lines', 'type': 'kpi', 'options': {}}, {'id': 'trend_value', 'title': 'Daily Order Value & Volume', 'type': 'combo', 'options': {'bars': ['qty'], 'span': 'wide', 'delta': -85.73, 'lines': ['amount'], '_locked': {'current': 52.8, 'previous': 370.0}, 'deltaLabel': 'vs prev. period'}}, {'id': 'top_products', 'title': 'Top Materials by Value', 'type': 'bar', 'options': {}}, {'id': 'top_customers', 'title': 'Top Customers by Value', 'type': 'bar', 'options': {}}, {'id': 'composition', 'title': 'Value Share by Material', 'type': 'pie', 'options': {}}, {'id': 'detail_table', 'title': 'Recent Order Detail', 'type': 'table', 'options': {}}], 'filters': [], 'insights': [{'body': "The last 30 days of order value are dominated by a single large booking on Aug 28 (≈59% of the week's value). Daily trend is spiky, not smooth — worth confirming whether this is a genuine bulk contract or a data-loading artifact.", 'title': 'Heavy Aug 28 concentration'}, {'body': 'Newest order record is 2026-08-31; no orders ingested for Sep 1–3. If upstream activity exists, the ERP-to-warehouse sync may be delayed.', 'title': 'Data lag since Aug 31'}, {'body': 'Daily Order Value & Volume moved 85.7% down vs the previous period (locked server-side from the live datasource at build time).', 'title': '📉 Daily Order Value & Volume'}], 'layout': [{'title': 'KPI Snapshot', 'widgets': ['kpi_value', 'kpi_volume', 'kpi_lines']}, {'title': 'Trend', 'widgets': ['trend_value']}, {'title': 'Breakdown', 'widgets': ['top_products', 'top_customers', 'composition']}, {'title': 'Detail', 'widgets': ['detail_table']}], 'pages': [], 'panels': [], 'header': None, 'footer': None}

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
