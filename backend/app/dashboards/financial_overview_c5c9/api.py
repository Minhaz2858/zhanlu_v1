"""Generated dashboard app router for `financial-overview-c5c9`. DO NOT EDIT.

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

SLUG = 'financial-overview-c5c9'
CONFIG = {'name': 'Financial Overview', 'slug': 'financial-overview-c5c9', 'description': 'Live revenue, expenses, gross margin and cash-flow metrics for C5/C9 project', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 300, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F172A', 'on_primary': '#FFFFFF', 'secondary': '#1E293B', 'accent': '#22C55E', 'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155', 'destructive': '#EF4444', 'ring': '#0F172A', 'chart_palette': ['#22C55E', '#EF4444', '#FFFFFF', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155'}}, 'typography': {'heading': 'IBM Plex Sans', 'body': 'IBM Plex Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Dark Mode (OLED)', 'keywords': 'Dark theme, low light, high contrast, deep black, midnight blue, eye-friendly, OLED, night mode, power efficient', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue_mtd', 'title': 'MTD Revenue (Aug 2026)', 'type': 'kpi', 'options': {}}, {'id': 'kpi_revenue_mom', 'title': 'Revenue MoM Change', 'type': 'kpi', 'options': {}}, {'id': 'kpi_contracted_qty', 'title': 'Contracted Volume (MTD)', 'type': 'kpi', 'options': {}}, {'id': 'kpi_delivery_rate', 'title': 'Delivery Rate (MTD)', 'type': 'kpi', 'options': {}}, {'id': 'trend_revenue_daily', 'title': 'Daily Revenue Trend (Jul–Aug 2026)', 'type': 'area', 'options': {'delta': -13.34, 'deltaLabel': 'vs prev. period', '_locked': {'current': 10396200.0, 'previous': 11996600.0}}}, {'id': 'trend_monthly_revenue', 'title': 'Monthly Revenue (Jan–Aug 2026)', 'type': 'bar', 'options': {'topItem': {'label': '2026-04', 'value': 404934020.533, 'share_pct': 17.5}}}, {'id': 'trend_monthly_volume', 'title': 'Monthly Contracted Volume (Tons)', 'type': 'bar', 'options': {'topItem': {'label': '2026-07', 'value': 50792.033, 'share_pct': 16.4}}}, {'id': 'breakdown_top_products', 'title': 'Top Products by Revenue (Jul–Aug)', 'type': 'pie', 'options': {'topItem': {'label': '抽余碳五', 'value': 113456955.41, 'share_pct': 18.3}}}, {'id': 'breakdown_org_revenue', 'title': 'Revenue by Organization (Aug 2026)', 'type': 'bar', 'options': {'topItem': {'label': '惠州伊斯科', 'value': 248657330.6588, 'share_pct': 87.3}}}, {'id': 'table_top_products_detail', 'title': 'Product Performance Detail (Jul–Aug)', 'type': 'table', 'options': {'topItem': {'label': '戊烷发泡剂', 'value': 15736.5, 'share_pct': 18.4}}}], 'filters': [], 'insights': [{'title': 'August Revenue Momentum', 'body': "MTD revenue of ¥301.8M trails July's ¥349.0M (-13.5%), but the month is still in progress. Contracted volume of 36,823 tons with 62.5% delivered signals strong pipeline headroom for the final week."}, {'title': 'HuiZhou ISCO Dominates', 'body': '惠州伊斯科 accounts for ¥248.7M (82.4%) of August revenue, with 广东伊斯科 contributing ¥36.2M (12.0%). Operations are highly concentrated in the Huizhou site.'}, {'title': '📉 Daily Revenue Trend (Jul–Aug 2026)', 'body': 'Daily Revenue Trend (Jul–Aug 2026) moved 13.3% down vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 2026-04', 'body': '2026-04 leads with 404,934,021 (17.5% of the total).'}, {'title': '🏆 Top: 2026-07', 'body': '2026-07 leads with 50,792 (16.4% of the total).'}], 'layout': []}

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
