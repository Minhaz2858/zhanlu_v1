"""Generated dashboard app router for `c5c9-live-market-reference`. DO NOT EDIT.

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

SLUG = 'c5c9-live-market-reference'
CONFIG = {'name': 'Live Market Reference — C5/C9', 'slug': 'c5c9-live-market-reference', 'description': 'Longzhong (隆众) market reference quotes for the C5/C9 complex: 裂解C5 / 裂解C9 spot with week-over-week movement, upstream feedstock context (naphtha/crude) and the spread vs ERP realized prices. Sourced from Longzhong market views — never mixed with ERP data.', 'theme': 'dark', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F766E', 'on_primary': '#FFFFFF', 'secondary': '#14B8A6', 'accent': '#0369A1', 'background': '#F0FDFA', 'foreground': '#134E4A', 'muted': '#E8F0F3', 'border': '#99F6E4', 'destructive': '#DC2626', 'ring': '#0F766E', 'chart_palette': ['#0F766E', '#0369A1', '#14B8A6', '#DC2626', '#2563eb', '#10b981'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Inter', 'body': 'Inter', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_c5', 'title': '裂解C5 均价 (Spot)', 'type': 'kpi', 'options': {'unit': '元/吨', 'delta': 7.9, 'deltaLabel': 'WoW', 'color': '#3B82F6'}}, {'id': 'kpi_c9', 'title': '裂解C9 均价 (Spot)', 'type': 'kpi', 'options': {'unit': '元/吨', 'delta': 8.8, 'deltaLabel': 'WoW', 'color': '#8B5CF6'}}, {'id': 'kpi_erp', 'title': 'ERP C5 Realized (Jul 2025)', 'type': 'kpi', 'options': {'unit': '元/吨', 'delta': -56.9, 'deltaLabel': 'vs spot', 'color': '#F59E0B'}}, {'id': 'kpi_spread', 'title': 'Spot vs ERP Spread', 'type': 'kpi', 'options': {'unit': '%', 'delta': 0, 'deltaLabel': 'ERP stale 229d', 'color': '#EF4444'}}, {'id': 'combo_upstream_vs_c5', 'title': 'Upstream Naphtha vs C5 Spot (Dual Axis)', 'type': 'combo', 'options': {'bars': ['c5'], 'lines': ['naphtha'], 'span': 'wide', 'barColor': '#3B82F6', 'lineColor': '#10B981'}}, {'id': 'line_c5_trend', 'title': '裂解C5 Spot Trend (Daily)', 'type': 'line', 'options': {'color': '#3B82F6', 'unit': '元/吨'}}, {'id': 'line_c9_trend', 'title': '裂解C9 Spot Trend (Daily)', 'type': 'line', 'options': {'color': '#8B5CF6', 'unit': '元/吨'}}, {'id': 'bar_supplier_latest', 'title': 'Latest Quote by Supplier (Tier Context)', 'type': 'bar', 'options': {'colors': ['#3B82F6', '#8B5CF6', '#10B981', '#F59E0B', '#EC4899', '#06B6D4']}}, {'id': 'table_quotes', 'title': 'Recent Daily Quotes', 'type': 'table', 'options': {}}], 'filters': [], 'insights': [{'title': 'C5 Spot', 'body': '裂解C5均价 is ¥6,824/t on the latest print (+7.9% WoW from ¥6,324). March saw a strong rally from ¥4,924 (03-02) to ¥6,824 (03-20), ~+38% — a genuine upstream-driven move, not noise.'}, {'title': 'C9 Spot', 'body': '裂解C9均价 trades at ¥6,200/t (+8.8% WoW). The C5–C9 gap of ¥624 reflects the cracker margin stack; watch it for downstream demand signals.'}, {'title': 'ERP Spread', 'body': 'ERP realized C5 (Jul 2025) averages ¥4,350/t — spot is +56.9% above it. The 229-day staleness makes this a lagging benchmark; treat the spread as directional, not absolute.'}]}

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
