"""Generated dashboard app router for `daily-sales-data-sync`. DO NOT EDIT.

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

SLUG = 'daily-sales-data-sync'
CONFIG = {'name': 'Daily Sales Data Sync', 'slug': 'daily-sales-data-sync', 'description': 'Real-time view of ERP sales sync activity: daily shipment volume, order-line activity, customer concentration, and anomaly flags across recent shipment windows.', 'theme': 'light', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#1E40AF', 'on_primary': '#FFFFFF', 'secondary': '#3B82F6', 'accent': '#D97706', 'background': '#F8FAFC', 'foreground': '#1E3A8A', 'muted': '#E9EEF6', 'border': '#DBEAFE', 'destructive': '#DC2626', 'ring': '#1E40AF', 'chart_palette': ['#1E40AF', '#D97706', '#3B82F6', '#DC2626', '#2563eb', '#10b981'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Sales Intelligence Dashboard', 'keywords': 'Deal pipeline, sales metrics, territory performance, sales rep leaderboard, win-loss analysis, quota tracking, forecast accuracy', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_latest_volume', 'title': 'Latest Day Volume', 'type': 'kpi', 'options': {'delta': 'vs prior day', 'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}, 'unit': 't'}}, {'id': 'kpi_latest_orders', 'title': 'Latest Day Order Lines', 'type': 'kpi', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}}, {'id': 'kpi_latest_customers', 'title': 'Active Customers', 'type': 'kpi', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}}, {'id': 'trend_volume', 'title': 'Daily Shipment Volume', 'type': 'line', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}, 'span': 'wide', 'unit': 't'}}, {'id': 'bar_order_activity', 'title': 'Daily Order-Line Activity', 'type': 'bar', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}}, {'id': 'pie_customers', 'title': 'Customer Share (Latest Day)', 'type': 'pie', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}}, {'id': 'table_recent', 'title': 'Recent Shipment Days', 'type': 'table', 'options': {'filters': {'date': {'column': 'shipment_date', 'op': '>=', 'value': ':dim_date'}}}}], 'filters': [], 'insights': [{'body': '3,957.5 t shipped on the most recent day versus 13,299.4 t the day before — a sharp single-day contraction consistent with a quiet year-end order book.', 'title': 'Latest volume −70% vs prior day'}, {'body': '客户C alone carried ~79% of latest-day volume (3,131 t of 3,958 t), leaving the book exposed to single-account swings.', 'title': 'Customer concentration high'}, {'body': 'Latest day sits ~67% under the trailing 8-day average (~11,852 t), flagging an anomaly worth watching in the next sync.', 'title': 'Volume below 8-day norm'}], 'layout': [{'title': 'Latest Sync Snapshot', 'widgets': ['kpi_latest_volume', 'kpi_latest_orders', 'kpi_latest_customers']}, {'title': 'Volume & Activity Trend', 'widgets': ['trend_volume', 'bar_order_activity']}, {'title': 'Breakdown & Detail', 'widgets': ['pie_customers', 'table_recent']}], 'pages': [], 'panels': [], 'header': None, 'footer': None}

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
