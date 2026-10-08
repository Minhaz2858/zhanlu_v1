"""Generated dashboard app router for `sales-performance-dashboard`. DO NOT EDIT.

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

SLUG = 'sales-performance-dashboard'
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-dashboard', 'description': '实时监控关键业务指标，涵盖总营收、订单量及核心数据变动趋势。动态分析不同区域的销售表现，清晰呈现市场地域分布全貌。精确追踪热销产品动态，实时掌握爆款商品与品类的销售走势。深度链接底层业务数据库，确保底层数据的真实性与高频同步更新。自动化更新与交互式分析相结合，助您快速精准制定高价值商业决策。', 'theme': 'light', 'style': 'standard', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#1E3A8A', 'on_primary': '#FFFFFF', 'secondary': '#3B82F6', 'accent': '#7C3AED', 'background': '#F8FAFC', 'foreground': '#1E40AF', 'muted': '#E9EEF5', 'border': '#BFDBFE', 'destructive': '#DC2626', 'ring': '#1E3A8A', 'chart_palette': ['#1E3A8A', '#7C3AED', '#3B82F6', '#DC2626', '#2563eb', '#10b981'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue', 'title': '总营收 (¥)', 'type': 'kpi', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'kpi_orders', 'title': '订单笔数', 'type': 'kpi', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'kpi_qty', 'title': '总销量 (吨)', 'type': 'kpi', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'kpi_customers', 'title': '客户数 (家)', 'type': 'kpi', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'trend_revenue', 'title': '营收日趋势 (¥)', 'type': 'line', 'options': {'span': 'wide', 'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'orders_trend', 'title': '订单量日趋势', 'type': 'line', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'region_pie', 'title': '区域营收分布', 'type': 'pie', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'category_pie', 'title': '品类营收分布', 'type': 'pie', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}, {'id': 'top_products', 'title': '热销产品 Top 10', 'type': 'bar', 'options': {'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization', 'values': ['示例工厂', '广东工厂']}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group', 'values': ['复合材料', '液体', 'C5原料', 'C5产品']}]}}], 'filters': [{'key': 'dim_org', 'label': '销售组织', 'column': 'organization'}, {'key': 'dim_category', 'label': '产品品类', 'column': 'product_group'}], 'insights': [{'body': '示例工厂与广东工厂双主体贡献全部销售，覆盖复合材料、液体、C5原料及C5产品四大品类，营收结构多元，可切换组织与品类维度下钻分析。', 'title': '双组织驱动营收'}, {'body': '复合材料为第一大品类主线，C5原料与C5产品构成上下游协同，品类间联动紧密，需关注单品价格波动传导。', 'title': '品类集中度高'}, {'body': '本看板以30秒为刷新周期，WebSocket推送底层订单数据变更，支持按区域/品类交叉筛选，支撑销售决策的实时响应。', 'title': '实时监控窗口'}], 'layout': [{'title': '核心指标总览', 'widgets': ['kpi_revenue', 'kpi_orders', 'kpi_qty', 'kpi_customers']}, {'title': '销售趋势分析', 'widgets': ['trend_revenue', 'orders_trend']}, {'title': '区域与品类分布', 'widgets': ['region_pie', 'category_pie', 'top_products']}], 'pages': [], 'panels': [], 'header': None, 'footer': None}

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
