"""Generated dashboard app router for `sales-performance-dashboard-3`. DO NOT EDIT.

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

SLUG = 'sales-performance-dashboard-3'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-dashboard-3', 'description': 'Live revenue, order volume, regional split and top-product trends from the bound business database', 'theme': 'light', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#059669', 'on_primary': '#FFFFFF', 'secondary': '#10B981', 'accent': '#DC2626', 'background': '#F8FAFC', 'foreground': '#0F172A', 'muted': '#F0F8F6', 'border': '#E1F2ED', 'destructive': '#DC2626', 'ring': '#059669', 'chart_palette': ['#059669', '#DC2626', '#10B981', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Financial Dashboard', 'keywords': 'Revenue metrics, profit/loss visualization, budget tracking, financial ratios, portfolio performance, cash flow, audit trail', 'card_radius': '8px'}}, 'metrics': [{'id': 'total_revenue', 'title': 'Total Revenue (Jul–Aug)', 'type': 'kpi', 'options': {}}, {'id': 'order_volume', 'title': 'Order Volume', 'type': 'kpi', 'options': {}}, {'id': 'order_qty', 'title': 'Contracted Quantity', 'type': 'kpi', 'options': {}}, {'id': 'active_customers', 'title': 'Active Customers', 'type': 'kpi', 'options': {}}, {'id': 'daily_revenue', 'title': 'Daily Revenue (Aug)', 'type': 'line', 'options': {'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}}, {'id': 'monthly_trend', 'title': 'Monthly Revenue (2026)', 'type': 'bar', 'options': {'topItem': {'label': '2026-04', 'value': 404934020.533, 'share_pct': 17.4}}}, {'id': 'regional_split', 'title': 'Regional Split (Aug)', 'type': 'pie', 'options': {'topItem': {'label': '惠州伊斯科', 'value': 270624538.4828, 'share_pct': 83.6}}}, {'id': 'top_products', 'title': 'Top Products (Aug)', 'type': 'bar', 'options': {'topItem': {'label': '抽余碳五', 'value': 68360034.51, 'share_pct': 21.1}}}], 'filters': [], 'insights': [{'title': 'Revenue momentum', 'body': 'Two-month revenue (Jul–Aug) totals ¥651.0M across 356 orders and 112 customers. August is tracking near July (¥323.8M MTD vs ¥349.0M) despite a mid-month trough, with a strong ¥50.3M day on Aug 21.'}, {'title': 'Regional concentration', 'body': '惠州伊斯科 drives 83.8% of August revenue (¥270.6M), with 广东伊斯科 contributing ¥36.2M. The business is heavily concentrated in a single operating entity.'}, {'title': '📈 Daily Revenue (Aug)', 'body': 'Daily Revenue (Aug) moved 8572.6% up vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 2026-04', 'body': '2026-04 leads with 404,934,021 (17.4% of the total).'}, {'title': '🏆 Top: 惠州伊斯科', 'body': '惠州伊斯科 leads with 270,624,538 (83.6% of the total).'}], 'layout': []}

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
