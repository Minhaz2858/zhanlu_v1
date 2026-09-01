"""Generated dashboard app router for `sales-performance-dashboard-pro-2`. DO NOT EDIT.

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

SLUG = 'sales-performance-dashboard-pro-2'
CONFIG = {'name': 'Sales Performance Dashboard — Trailing 30 Days', 'slug': 'sales-performance-dashboard-pro-2', 'description': 'Live total revenue with 30 / 15 / 7-day views, daily revenue trend, regional split and top products. WebSocket live updates every 30s.', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F172A', 'on_primary': '#FFFFFF', 'secondary': '#1E293B', 'accent': '#22C55E', 'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155', 'destructive': '#EF4444', 'ring': '#0F172A', 'chart_palette': ['#22C55E', '#EF4444', '#FFFFFF', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155'}}, 'typography': {'heading': 'IBM Plex Sans', 'body': 'IBM Plex Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue_30d', 'title': 'Total Revenue — Last 30 Days', 'type': 'kpi', 'options': {'unit': '¥', 'format': 'currency', 'caption': 'Last 30 days (Jul 30 – Aug 28)'}}, {'id': 'kpi_revenue_15d', 'title': 'Total Revenue — Last 15 Days', 'type': 'kpi', 'options': {'unit': '¥', 'format': 'currency', 'caption': 'Last 15 days (Aug 14 – Aug 28)'}}, {'id': 'kpi_revenue_7d', 'title': 'Total Revenue — Last 7 Days', 'type': 'kpi', 'options': {'unit': '¥', 'format': 'currency', 'caption': 'Last 7 days (Aug 22 – Aug 28)'}}, {'id': 'kpi_orders_30d', 'title': 'Order Volume — 30 Days', 'type': 'kpi', 'options': {'format': 'number', 'caption': 'Distinct orders, last 30 days'}}, {'id': 'trend_daily', 'title': 'Daily Revenue — Trailing 30 Days', 'type': 'area', 'options': {'x': 'd', 'y': 'revenue', 'area': True, 'unit': '¥', 'color': '#22C55E', 'delta': 8572.57, 'format': 'currency', '_locked': {'current': 21716800.0, 'previous': 250407.824}, 'caption': 'Daily revenue, Jul 30 – Aug 28', 'deltaLabel': 'vs prev. period'}}, {'id': 'split_region', 'title': 'Regional Split — Last 30 Days', 'type': 'pie', 'options': {'unit': '¥', 'label': 'org_name', 'value': 'revenue', 'format': 'currency', 'caption': 'Revenue by operating entity, last 30 days', 'topItem': {'label': '惠州伊斯科', 'value': 299925731.4948, 'share_pct': 79.8}}}, {'id': 'top_products', 'title': 'Top Products — Last 30 Days', 'type': 'bar', 'options': {'x': 'material_name', 'y': 'revenue', 'unit': '¥', 'color': '#22C55E', 'format': 'currency', 'caption': 'Top 10 products, last 30 days', 'topItem': {'label': '戊烷发泡剂', 'value': 77676050.0, 'share_pct': 20.7}, 'horizontal': True}}, {'id': 'top_customers', 'title': 'Top Customers — Last 30 Days', 'type': 'bar', 'options': {'x': 'CUST_NAME', 'y': 'revenue', 'unit': '¥', 'color': '#38BDF8', 'format': 'currency', 'caption': 'Top 8 customers, last 30 days', 'topItem': {'label': '中海壳牌石油化工有限公司', 'value': 68360034.51, 'share_pct': 30.5}, 'horizontal': True}}], 'filters': [], 'insights': [{'body': '¥376.0M across 192 orders and 77 customers (Jul 30 – Aug 28). The trailing 15 days total ¥132.0M and the trailing 7 days ¥50.5M.', 'title': 'Total revenue — last 30 days'}, {'body': '¥50.3M in a single day was the 30-day high, followed by Aug 11 (¥48.4M) and Aug 5 (¥43.6M). These three days alone account for ~38% of the 30-day total.', 'title': 'Peak day: Aug 21'}, {'body': 'Daily revenue swings from ¥140.7k (Aug 22) to ¥50.3M (Aug 21) — a 350x range. This reflects lumpy order booking rather than steady flow, so 7-day windows can be misleading and 30-day totals are the more stable signal.', 'title': 'Revenue is spiky, not smooth'}], 'layout': []}

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
