"""Generated dashboard app router for `sales-perf-dashboard-c5c9`. DO NOT EDIT.

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

SLUG = 'sales-perf-dashboard-c5c9'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-perf-dashboard-c5c9', 'description': '实时监控关键业务指标，涵盖总营收、订单量及核心数据变动趋势。动态分析不同区域的销售表现。精确追踪热销产品动态。深度链接底层业务数据库，确保底层数据的真实性与高频同步更新。', 'theme': 'dark', 'style': 'chinese_bi', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#1E3A8A', 'on_primary': '#FFFFFF', 'secondary': '#3B82F6', 'accent': '#7C3AED', 'background': '#F8FAFC', 'foreground': '#1E40AF', 'muted': '#E9EEF5', 'border': '#BFDBFE', 'destructive': '#DC2626', 'ring': '#1E3A8A', 'chart_palette': ['#1E3A8A', '#7C3AED', '#3B82F6', '#DC2626', '#2563eb', '#10b981'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue', 'title': '总营收（净额）', 'type': 'kpi', 'options': {'unit': '¥'}}, {'id': 'kpi_orders', 'title': '订单量', 'type': 'kpi', 'options': {'unit': '单'}}, {'id': 'kpi_tons', 'title': '交付量', 'type': 'kpi', 'options': {'unit': '吨'}}, {'id': 'kpi_customers', 'title': '活跃客户', 'type': 'kpi', 'options': {'unit': '家'}}, {'id': 'trend_revenue_tons', 'title': '月度营收与销量趋势（2025-2026）', 'type': 'combo', 'options': {'bars': ['revenue'], 'lines': ['tons'], 'span': 'wide', 'x': 'ym', 'delta': -6.6, 'deltaLabel': 'vs prev. period', '_locked': {'current': 184.0, 'previous': 197.0}}}, {'id': 'region_pie', 'title': '区域营收占比（2026 YTD）', 'type': 'pie', 'options': {'topItem': {'label': '惠州伊斯科', 'value': 1657921227.56, 'share_pct': 79.4}}}, {'id': 'region_trend', 'title': '区域对比：营收 vs 销量', 'type': 'bar', 'options': {'x': 'region', 'topItem': {'label': '惠州伊斯科', 'value': 1657921227.56, 'share_pct': 79.4}}}, {'id': 'product_bar', 'title': '热销产品 Top 12（2026 YTD 营收）', 'type': 'bar', 'options': {'x': 'product', 'topItem': {'label': '异戊二烯', 'value': 463655686.14, 'share_pct': 22.5}}}, {'id': 'recent_orders_table', 'title': '最新订单明细（2026-08）', 'type': 'table', 'options': {'topItem': {'label': '2026-08-28', 'value': 286.33, 'share_pct': 7.8}}}], 'filters': [], 'insights': [{'body': '截至2026-08-31，全年累计含税订单额 ¥23.49亿、净额 ¥20.87亿，已交付 28.06万吨。8月单月营收 ¥3.03亿，环比7月 ¥3.13亿小幅回落 3.2%，但仍处年内高位区间。', 'title': '2026 YTD 营收 ¥20.87亿，同比增速强劲'}, {'body': '惠州伊斯科 YTD ¥16.58亿（占79%），广东伊斯科 ¥4.05亿（占19%，增速最快）。区域集中度高，广东基地是下一个增长引擎。', 'title': '惠州伊斯科贡献 79% 营收，双基地格局成型'}, {'title': '📉 月度营收与销量趋势（2025-2026）', 'body': '月度营收与销量趋势（2025-2026） moved 6.6% down vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 惠州伊斯科', 'body': '惠州伊斯科 leads with 1,657,921,228 (79.4% of the total).'}, {'title': '🏆 Top: 惠州伊斯科', 'body': '惠州伊斯科 leads with 1,657,921,228 (79.4% of the total).'}], 'layout': [{'title': '核心指标 KPI', 'widgets': ['kpi_revenue', 'kpi_orders', 'kpi_tons', 'kpi_customers']}, {'title': '营收与销量趋势', 'widgets': ['trend_revenue_tons']}, {'title': '区域与产品分布', 'widgets': ['region_pie', 'product_bar']}, {'title': '最新订单明细', 'widgets': ['recent_orders_table']}], 'pages': [], 'panels': [], 'header': None, 'footer': None}

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
