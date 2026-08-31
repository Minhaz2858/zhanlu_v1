"""Generated dashboard app router for `sales-performance-dashboard-pro`. DO NOT EDIT.

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

SLUG = 'sales-performance-dashboard-pro'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-dashboard-pro', 'description': 'Advanced executive BI: live revenue, order volume, regional split, top products, customer concentration, delivery performance and price trends from the C5/C9 business database. WebSocket live updates every 30s.', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F172A', 'on_primary': '#FFFFFF', 'secondary': '#1E293B', 'accent': '#22C55E', 'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155', 'destructive': '#EF4444', 'ring': '#0F172A', 'chart_palette': ['#22C55E', '#EF4444', '#FFFFFF', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155'}}, 'typography': {'heading': 'IBM Plex Sans', 'body': 'IBM Plex Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue', 'title': 'Total Revenue (Jul–Aug)', 'type': 'kpi', 'options': {'unit': '¥', 'format': 'currency', 'caption': 'Tax-inclusive sales, Jul 1 – Aug 28 2026'}}, {'id': 'kpi_orders', 'title': 'Order Volume', 'type': 'kpi', 'options': {'format': 'number', 'caption': 'Distinct sales orders, Jul–Aug'}}, {'id': 'kpi_qty', 'title': 'Contracted Quantity', 'type': 'kpi', 'options': {'unit': 't', 'format': 'number', 'caption': 'Total contracted tonnage, Jul–Aug'}}, {'id': 'kpi_customers', 'title': 'Active Customers', 'type': 'kpi', 'options': {'format': 'number', 'caption': 'Distinct buyers, Jul–Aug'}}, {'id': 'trend_daily', 'title': 'Daily Revenue — August 2026', 'type': 'line', 'options': {'x': 'd', 'y': 'revenue', 'unit': '¥', 'format': 'currency', 'color': '#22C55E', 'area': True, 'caption': 'Revenue per day, Aug 1 – 28', 'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}}, {'id': 'trend_monthly', 'title': 'Monthly Revenue — 2026', 'type': 'bar', 'options': {'x': 'mon', 'y': 'revenue', 'unit': '¥', 'format': 'currency', 'color': '#22C55E', 'caption': 'Revenue by month, Jan–Aug 2026', 'topItem': {'label': '2026-04', 'value': 404934020.533, 'share_pct': 17.4}}}, {'id': 'split_region', 'title': 'Regional Split — August', 'type': 'pie', 'options': {'label': 'org_name', 'value': 'revenue', 'unit': '¥', 'format': 'currency', 'caption': 'Revenue by operating entity', 'topItem': {'label': '惠州伊斯科', 'value': 270624538.4828, 'share_pct': 83.6}}}, {'id': 'top_products', 'title': 'Top Products — August', 'type': 'bar', 'options': {'x': 'material_name', 'y': 'revenue', 'unit': '¥', 'format': 'currency', 'horizontal': True, 'color': '#22C55E', 'caption': 'Top 10 products by revenue', 'topItem': {'label': '抽余碳五', 'value': 68360034.51, 'share_pct': 21.1}}}, {'id': 'top_customers', 'title': 'Top Customers — August', 'type': 'bar', 'options': {'x': 'CUST_NAME', 'y': 'revenue', 'unit': '¥', 'format': 'currency', 'horizontal': True, 'color': '#38BDF8', 'caption': 'Top 8 customers by revenue', 'topItem': {'label': '中海壳牌石油化工有限公司', 'value': 68360034.51, 'share_pct': 33.1}}}, {'id': 'delivery_perf', 'title': 'Delivery Performance by Product Group', 'type': 'combo', 'options': {'x': 'material_group', 'y': 'delivered', 'y2': 'contracted', 'unit': 't', 'caption': 'Delivered vs contracted tonnage (t)', 'delta': -57.1, 'deltaLabel': 'vs prev. period', '_locked': {'current': 532.0, 'previous': 1240.0}}}, {'id': 'price_trend', 'title': 'Price Snapshot by Product — August', 'type': 'table', 'options': {'caption': 'Avg / max / min tax-inclusive price (¥/t) and order count per product', 'topItem': {'label': None, 'value': 13725.0, 'share_pct': 17.4}}}], 'filters': [], 'insights': [{'title': 'Revenue momentum', 'body': 'August revenue is ¥323.8M vs July ¥349.0M — tracking ~93% of July with 3 days still remaining this month, so August is on pace to match or slightly exceed July. Strongest days: Aug 21 (¥50.3M) and Aug 11 (¥48.4M).'}, {'title': 'Regional concentration risk', 'body': '惠州伊斯科 accounts for ¥270.6M, or 83.8% of August revenue. This single-entity concentration is a key dependency risk — a production or logistics disruption there would hit the vast majority of revenue.'}, {'title': '📈 Daily Revenue — August 2026', 'body': 'Daily Revenue — August 2026 moved 8572.6% up vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 2026-04', 'body': '2026-04 leads with 404,934,021 (17.4% of the total).'}, {'title': '🏆 Top: 惠州伊斯科', 'body': '惠州伊斯科 leads with 270,624,538 (83.6% of the total).'}], 'layout': []}

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
