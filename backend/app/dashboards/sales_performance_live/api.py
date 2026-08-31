"""Generated dashboard app router for `sales-performance-live`. DO NOT EDIT.

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

SLUG = 'sales-performance-live'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-live', 'description': 'Chinese BI style, Live revenue, order volume, regional split and top-product trends from the bound business database', 'theme': 'dark', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/C5_C9/MASTER.md', 'design': {}, 'metrics': [{'id': 'kpi_revenue', 'title': '实时销售额', 'type': 'kpi', 'options': {'compare_label': '2026年至今', 'precision': 0, 'unit': '¥'}}, {'id': 'kpi_orders', 'title': '订单量', 'type': 'kpi', 'options': {'compare_label': '2026年至今', 'precision': 0, 'unit': '单'}}, {'id': 'kpi_qty', 'title': '销量', 'type': 'kpi', 'options': {'compare_label': '2026年至今', 'precision': 1, 'unit': '吨'}}, {'id': 'kpi_avg_order', 'title': '客单价', 'type': 'kpi', 'options': {'compare_label': '单均成交额', 'precision': 0, 'unit': '¥'}}, {'id': 'trend_revenue', 'title': '月度销售趋势', 'type': 'area', 'options': {'stacked': False, 'x': 'month', 'y': 'revenue', 'delta': 123.07, 'deltaLabel': 'vs prev. period', '_locked': {'current': 348986022.3273, 'previous': 156444806.9482}}}, {'id': 'trend_daily', 'title': '日销售趋势', 'type': 'line', 'options': {'x': 'day', 'y': 'revenue', 'delta': -13.34, 'deltaLabel': 'vs prev. period', '_locked': {'current': 10396200.0, 'previous': 11996600.0}}}, {'id': 'split_org', 'title': '区域销售占比', 'type': 'pie', 'options': {'label': 'org', 'value': 'revenue', 'topItem': {'label': '惠州伊斯科', 'value': 1814050075.0719, 'share_pct': 78.6}}}, {'id': 'top_materials', 'title': 'TOP10 产品销售额', 'type': 'bar', 'options': {'horizontal': True, 'x': 'material', 'y': 'revenue', 'topItem': {'label': '异戊二烯', 'value': 509887532.5906, 'share_pct': 22.4}}}, {'id': 'top_customers', 'title': 'TOP10 客户', 'type': 'table', 'options': {'topItem': {'label': '中国石化化工销售有限公司华中分公司', 'value': 294861494.05, 'share_pct': 23.4}}}, {'id': 'region_trend', 'title': '区域月度对比', 'type': 'combo', 'options': {'series': 'org', 'x': 'month', 'y': 'revenue', 'delta': -85.45, 'deltaLabel': 'vs prev. period', '_locked': {'current': 36182700.0, 'previous': 248657330.6588}}}], 'filters': [], 'insights': [{'body': '截至2026年8月，累计销售额约23.08亿元，其中惠州伊斯科贡献约78.6%的营收（18.14亿），广东伊斯科约20.2%（4.67亿）。', 'title': '2026年累计销售额'}, {'body': 'TOP3产品（异戊二烯、工业用裂解碳五、碳五石油树脂）合计贡献超12.8亿元，占全年营收的55%以上，产品结构集中风险需关注。', 'title': '主力产品集中度高'}, {'title': '📈 月度销售趋势', 'body': '月度销售趋势 moved 123.1% up vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '📉 日销售趋势', 'body': '日销售趋势 moved 13.3% down vs the previous period (locked server-side from the live datasource at build time).'}, {'title': '🏆 Top: 惠州伊斯科', 'body': '惠州伊斯科 leads with 1,814,050,075 (78.6% of the total).'}], 'layout': []}

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
