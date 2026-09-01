"""Generated dashboard app router for `sales-performance-dashboard-v2`. DO NOT EDIT.

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

SLUG = 'sales-performance-dashboard-v2'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-dashboard-v2', 'description': 'Live revenue, order volume, regional split and top-product trends from the bound business database.', 'theme': 'dark', 'style': 'chinese_bi', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#059669', 'on_primary': '#FFFFFF', 'secondary': '#10B981', 'accent': '#DC2626', 'background': '#F8FAFC', 'foreground': '#0F172A', 'muted': '#F0F8F6', 'border': '#E1F2ED', 'destructive': '#DC2626', 'ring': '#059669', 'chart_palette': ['#059669', '#DC2626', '#10B981', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'total_revenue', 'title': 'Total Revenue (2026)', 'type': 'kpi', 'options': {}}, {'id': 'total_orders', 'title': 'Total Orders', 'type': 'kpi', 'options': {}}, {'id': 'total_volume', 'title': 'Total Volume (t)', 'type': 'kpi', 'options': {}}, {'id': 'delivered_volume', 'title': 'Delivered (t)', 'type': 'kpi', 'options': {}}, {'id': 'revenue_trend', 'title': 'Revenue Trend (Monthly)', 'type': 'line', 'options': {'delta': -13.52, '_locked': {'current': 301796030.6588, 'previous': 348986022.3273}, 'deltaLabel': 'vs prev. period'}}, {'id': 'order_trend', 'title': 'Order Volume Trend', 'type': 'line', 'options': {'delta': -20.3, '_locked': {'current': 157.0, 'previous': 197.0}, 'deltaLabel': 'vs prev. period'}}, {'id': 'org_split', 'title': 'Revenue by Organization', 'type': 'pie', 'options': {'topItem': {'label': '惠州伊斯科', 'value': 1814050075.0719, 'share_pct': 78.6}}}, {'id': 'top_products', 'title': 'Top Products by Revenue', 'type': 'bar', 'options': {'topItem': {'label': '异戊二烯', 'value': 509887532.5906, 'share_pct': 22.2}}}, {'id': 'product_volume', 'title': 'Product Volume (t)', 'type': 'bar', 'options': {'topItem': {'label': '工业用裂解碳五', 'value': 80138.06, 'share_pct': 26.0}}}, {'id': 'latest_orders', 'title': 'Latest Orders', 'type': 'table', 'options': {'topItem': {'label': '2026-08-26T09:53:32', 'value': 280.0, 'share_pct': 22.0}}}], 'filters': [], 'insights': [{'body': 'Revenue Trend (Monthly) moved 13.5% down vs the previous period (locked server-side from the live datasource at build time).', 'title': '📉 Revenue Trend (Monthly)'}, {'body': 'Order Volume Trend moved 20.3% down vs the previous period (locked server-side from the live datasource at build time).', 'title': '📉 Order Volume Trend'}, {'body': '惠州伊斯科 leads with 1,814,050,075 (78.6% of the total).', 'title': '🏆 Top: 惠州伊斯科'}], 'layout': [{'title': 'KPI Overview', 'widgets': ['total_revenue', 'total_quantity', 'active_orders', 'avg_price']}, {'title': 'Trends', 'widgets': ['revenue_trend', 'order_trend']}, {'title': 'Breakdown', 'widgets': ['org_split', 'top_products', 'product_volume', 'latest_orders']}]}

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
