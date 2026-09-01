"""Generated dashboard app router for `c5_c9_financial_overview`. DO NOT EDIT.

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

SLUG = 'c5_c9_financial_overview'
CONFIG = {'name': 'Financial Overview', 'slug': 'c5_c9_financial_overview', 'description': 'Live revenue, expenses, gross margin and cash-flow metrics', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F172A', 'on_primary': '#FFFFFF', 'secondary': '#1E293B', 'accent': '#22C55E', 'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155', 'destructive': '#EF4444', 'ring': '#0F172A', 'chart_palette': ['#22C55E', '#EF4444', '#FFFFFF', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'revenue_total', 'title': 'Total Revenue (Tax-Incl)', 'type': 'kpi', 'options': {}}, {'id': 'revenue_30d_trend', 'title': 'Revenue Trend (30 Days)', 'type': 'line', 'options': {'delta': 8572.57, 'deltaLabel': 'vs prev. period', '_locked': {'current': 21716800.0, 'previous': 250407.824}}}, {'id': 'revenue_by_product', 'title': 'Revenue by Product', 'type': 'bar', 'options': {'topItem': {'label': '异戊二烯', 'value': 88471606.2878, 'share_pct': 21.7}}}, {'id': 'top_customers', 'title': 'Top Customers (30D)', 'type': 'table', 'options': {'topItem': {'label': '中海壳牌石油化工有限公司', 'value': 8649.0, 'share_pct': 25.5}}}], 'filters': [], 'insights': [{'title': 'Revenue pulse', 'body': 'Tracks tax-inclusive sales amount from the ERP sales-order view over the trailing 30 days, refreshed every 30s via WebSocket.'}, {'title': 'Product mix', 'body': 'Top products by revenue are ranked to show which C5/C9 lines drive the top line.'}, {'title': '📈 Revenue Trend (30 Days)', 'body': 'Revenue Trend (30 Days) moved 8572.6% up vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 异戊二烯', 'body': '异戊二烯 leads with 88,471,606 (21.7% of the total).'}, {'title': '🏆 Top: 中海壳牌石油化工有限公司', 'body': '中海壳牌石油化工有限公司 leads with 8,649 (25.5% of the total).'}], 'layout': []}

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
