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
CONFIG = {'name': 'Sales Performance Dashboard', 'slug': 'sales-performance-dashboard', 'description': 'Live revenue, order volume, delivery progress, tax, product mix, customer and organization splits from the bound business ERP — professional executive view.', 'theme': 'dark', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#059669', 'on_primary': '#FFFFFF', 'secondary': '#10B981', 'accent': '#DC2626', 'background': '#F8FAFC', 'foreground': '#0F172A', 'muted': '#F0F8F6', 'border': '#E1F2ED', 'destructive': '#DC2626', 'ring': '#059669', 'chart_palette': ['#059669', '#DC2626', '#10B981', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#f8fafc', 'muted': '#1e293b', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Data-Dense Dashboard', 'keywords': 'Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility', 'card_radius': '8px'}}, 'metrics': [{'id': 'total_revenue', 'title': 'Total Revenue (Incl. Tax)', 'type': 'kpi', 'options': {'prefix': '¥', 'format': 'compact'}}, {'id': 'order_volume', 'title': 'Order Volume (Tons)', 'type': 'kpi', 'options': {'suffix': ' t'}}, {'id': 'order_count', 'title': 'Distinct Orders', 'type': 'kpi', 'options': {}}, {'id': 'avg_price', 'title': 'Avg Unit Price', 'type': 'kpi', 'options': {'prefix': '¥'}}, {'id': 'delivery_rate', 'title': 'Delivery Fulfillment Rate', 'type': 'kpi', 'options': {'suffix': '%'}}, {'id': 'tax_amount', 'title': 'Tax Amount', 'type': 'kpi', 'options': {'prefix': '¥'}}, {'id': 'revenue_trend', 'title': 'Revenue Trend (30d)', 'type': 'area', 'options': {}}, {'id': 'volume_trend', 'title': 'Order Volume Trend (30d)', 'type': 'line', 'options': {}}, {'id': 'top_products', 'title': 'Top Products by Revenue', 'type': 'bar', 'options': {}}, {'id': 'product_mix', 'title': 'Revenue by Material Group', 'type': 'pie', 'options': {}}, {'id': 'customer_split', 'title': 'Top Customers by Revenue', 'type': 'bar', 'options': {}}, {'id': 'org_split', 'title': 'Revenue by Organization', 'type': 'pie', 'options': {}}, {'id': 'unit_split', 'title': 'Revenue by Unit of Measure', 'type': 'bar', 'options': {}}, {'id': 'recent_orders', 'title': 'Recent Orders', 'type': 'table', 'options': {}}], 'filters': [], 'insights': [{'body': 'Total revenue (incl. tax) of ¥386.5M across 188 orders in the trailing 30 days, with 47,804 tons booked at ¥7,502 avg unit price. Delivery fulfillment runs near the top of the book.', 'title': 'Revenue & Volume Pulse'}, {'body': 'The 液体 (liquid) material group drives ¥266.3M (69% of revenue). Top products 异戊二烯 (¥88.5M), 戊烷发泡剂 (¥77.3M) and 抽余碳五 (¥68.4M) concentrate ~60% of sales.', 'title': 'Product Concentration'}, {'body': 'Chinese petrochemical majors dominate: Sinopec Central China (¥70.2M) and CNOOC & Shell (¥68.4M) together approach 36% of revenue. 惠州伊斯科 org leads at ¥310.4M.', 'title': 'Customer & Org Split'}, {'body': 'Daily revenue is lumpy — peaking at ¥50.3M on Aug 21 and ¥48.4M on Aug 11 versus a ~¥14M daily average, indicating order-booking concentration worth monitoring.', 'title': 'Daily Volatility'}]}

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
