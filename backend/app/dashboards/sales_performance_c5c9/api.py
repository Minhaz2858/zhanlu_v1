"""Generated dashboard app router for `sales-performance-c5c9`. DO NOT EDIT.

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

SLUG = 'sales-performance-c5c9'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-c5c9', 'description': 'Live revenue, order volume, regional split and top-product trends from the bound business database', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#059669', 'on_primary': '#FFFFFF', 'secondary': '#10B981', 'accent': '#DC2626', 'background': '#F8FAFC', 'foreground': '#0F172A', 'muted': '#F0F8F6', 'border': '#E1F2ED', 'destructive': '#DC2626', 'ring': '#059669', 'chart_palette': ['#059669', '#DC2626', '#10B981', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Dark Mode (OLED)', 'keywords': 'Dark theme, low light, high contrast, deep black, midnight blue, eye-friendly, OLED, night mode, power efficient', 'card_radius': '8px'}}, 'metrics': [{'id': 'total_revenue', 'title': 'Total Revenue (Aug MTD)', 'type': 'kpi', 'options': {}}, {'id': 'total_orders', 'title': 'Order Volume (Aug MTD)', 'type': 'kpi', 'options': {}}, {'id': 'avg_order_value', 'title': 'Avg Order Value (¥)', 'type': 'kpi', 'options': {}}, {'id': 'delivery_rate', 'title': 'Delivery Completion %', 'type': 'kpi', 'options': {}}, {'id': 'mom_growth', 'title': 'Revenue MoM Growth %', 'type': 'kpi', 'options': {}}, {'id': 'revenue_trend', 'title': 'Daily Revenue Trend', 'type': 'area', 'options': {'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}}, {'id': 'delivery_trend', 'title': 'Daily Delivery Quantity', 'type': 'line', 'options': {'delta': 1077.29, 'deltaLabel': 'vs prev. period', '_locked': {'current': 329.64, 'previous': 28.0}}}, {'id': 'order_trend', 'title': 'Daily Order Volume', 'type': 'bar', 'options': {'topItem': {'label': '2026-08-13', 'value': 19.0, 'share_pct': 11.6}}}, {'id': 'regional_split', 'title': 'Regional Revenue Split', 'type': 'pie', 'options': {'topItem': {'label': '惠州伊斯科', 'value': 270624538.4828, 'share_pct': 83.6}}}, {'id': 'top_products', 'title': 'Top Products by Revenue', 'type': 'bar', 'options': {'topItem': {'label': '抽余碳五', 'value': 68360034.51, 'share_pct': 21.3}}}], 'filters': [], 'insights': [{'title': 'Strong MoM Growth', 'body': 'August revenue (¥288.3M MTD through 08-28) is up +22.4% vs the same July period (¥235.5M) — a +¥52.8M gain, driven mainly by Huizhou Isco.'}, {'title': 'Top Customer', 'body': '中海壳牌石油化工 is the largest account at ¥60.5M (~21% of August revenue); 北京万邦达新材料 leads order count with 15 orders.'}, {'title': 'Delivery Backlog', 'body': 'Delivery completion sits at 62.1% (24,706 t of 39,769 t contracted). Deliveries peaked Aug 5 (5,901 t) and Aug 13 (3,285 t), tapering late month.'}], 'layout': []}

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
