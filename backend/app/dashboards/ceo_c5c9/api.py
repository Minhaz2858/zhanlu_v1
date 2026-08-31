"""Generated dashboard app router for `ceo-c5c9`. DO NOT EDIT.

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

SLUG = 'ceo-c5c9'
CONFIG = {'name': 'CEO', 'slug': 'ceo-c5c9', 'description': 'One-screen command view for the CEO. Answers in under 30 seconds: how are we doing, where is the money coming from, what is going wrong, and what should I do about it? Decision-grade numbers, not data dumps. Refreshed daily from live ERP.', 'theme': 'dark', 'style': 'ceo', 'refresh_interval_seconds': 30, 'design_system_ref': 'design-system/default-org/MASTER.md', 'design': {'colors': {'primary': '#0F172A', 'on_primary': '#FFFFFF', 'secondary': '#1E293B', 'accent': '#22C55E', 'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155', 'destructive': '#EF4444', 'ring': '#0F172A', 'chart_palette': ['#22C55E', '#EF4444', '#FFFFFF', '#2563eb', '#10b981', '#f59e0b'], 'dark': {'background': '#020617', 'foreground': '#F8FAFC', 'muted': '#1A1E2F', 'border': '#334155'}}, 'typography': {'heading': 'Fira Code', 'body': 'Fira Sans', 'google_fonts_url': 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap', 'css_import': "@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600;700&family=Fira+Sans:wght@300;400;500;600;700&display=swap');"}, 'spacing': {'xs': '2px', 'sm': '4px', 'md': '8px', 'lg': '12px', 'xl': '16px', '2xl': '24px', '3xl': '32px'}, 'style': {'name': 'Executive Dashboard', 'keywords': 'High-level KPIs, large key metrics, minimal detail, summary view, trend indicators, at-a-glance insights, executive summary', 'card_radius': '8px'}}, 'metrics': [{'id': 'kpi_revenue', 'title': 'Revenue (excl VAT)', 'type': 'kpi', 'options': {'unit': '¥M', 'delta': True, 'filters': [{'key': 'product', 'label': 'Product', 'column': 'material_name'}], 'sparkline': True}}, {'id': 'kpi_volume', 'title': 'Volume (ordered)', 'type': 'kpi', 'options': {'unit': 't', 'delta': True, 'sparkline': True}}, {'id': 'kpi_price', 'title': 'Weighted Avg Price', 'type': 'kpi', 'options': {'unit': '¥/t', 'delta': True}}, {'id': 'kpi_delivery', 'title': 'Delivery Rate', 'type': 'kpi', 'options': {'unit': '%', 'delta': True}}, {'id': 'kpi_ytd', 'title': 'YTD Revenue', 'type': 'kpi', 'options': {'unit': '¥M', 'delta': True}}, {'id': 'trend_revenue_12m', 'title': 'Revenue Trend (12 months)', 'type': 'line', 'options': {'span': 'wide', 'x_key': 'period', 'y_keys': ['revenue_m']}}, {'id': 'bar_products', 'title': 'Top Products by Revenue', 'type': 'bar', 'options': {'x_key': 'material_name', 'y_keys': ['revenue_m']}}, {'id': 'table_customers', 'title': 'Top Customers', 'type': 'table', 'options': {}}], 'filters': [{'key': 'product', 'label': 'Product', 'column': 'material_name'}], 'insights': [{'body': 'July revenue ¥311.0M (+122.5% MoM) driven by +154.7% volume growth to 50,792t. Mix shifted toward lower-priced products, dragging weighted avg price down 12.6% to ¥6,123/t.', 'title': 'Record month — volume-driven growth'}, {'body': 'Only 390.5t of 3,000t ordered delivered (13%), ~¥12.9M revenue at risk. Expedite fulfillment or renegotiate schedule.', 'title': 'Delivery gap — 惠州伊斯科 revenue at risk'}, {'body': 'Revenue (excl VAT) moved 1.0% up vs the previous period (locked server-side from the live datasource at build time).', 'title': '📈 Revenue (excl VAT)'}, {'body': 'Volume (ordered) moved 1.0% up vs the previous period (locked server-side from the live datasource at build time).', 'title': '📈 Volume (ordered)'}, {'body': 'Weighted Avg Price moved 1.0% up vs the previous period (locked server-side from the live datasource at build time).', 'title': '📈 Weighted Avg Price'}], 'layout': [{'title': 'KPI Overview', 'widgets': ['kpi_revenue', 'kpi_volume', 'kpi_avg_price', 'kpi_delivery', 'kpi_ytd']}, {'title': 'Revenue Trend', 'widgets': ['trend_revenue']}, {'title': 'Market Position & Alerts', 'page': 'market', 'widgets': ['prod_ranking'], 'panels': ['alerts-rail', 'chain-value', 'market-narrative']}, {'title': 'Customer Ranking', 'widgets': ['cust_ranking']}], 'pages': [{'id': 'overview', 'label': 'CEO 总览'}, {'id': 'market', 'label': '市场行情'}], 'panels': [{'id': 'alerts-rail', 'type': 'alerts', 'span': 'full', 'items': [{'severity': 'warn', 'icon': 'alert-triangle', 'title': 'Delivery rate at 87.3% — 27 orders lack material/org tags', 'body': 'Aggregate 2026 delivery rate is 87.3% (FDELIQTY/FQTY_ORIGIN). 27 orders have null material_name or org_name — a master-data gap that obscures ~revenue attribution. Fix ERP tagging to stop revenue leakage.', 'cta': 'Audit 27 untagged orders →', 'time': 'daily'}, {'severity': 'info', 'icon': 'trending-up', 'title': 'All 6 market products rising MoM — DCPD leads at +17.7%', 'body': 'Longzhong reference prices (erp_sale_order_weighted) up across the board: DCPD 6559.9 (+17.7%), blowing_agent 6194.7 (+12.9%), cracked_c9 3008.9 (+9.7%), isoprene 10619.5 (+5.8%), cracked_c5 5531.0 (+5.6%), piperylene 6592.9 (+3.5%). Rising feedstock costs pressure margins.', 'cta': 'Review pricing vs market →', 'time': '2026-08-28'}, {'severity': 'info', 'icon': 'refresh', 'title': 'Market data fresh — latest 2026-08-28 (1 day old)', 'body': "3 of 6 products (blowing_agent, cracked_c9, dcpd) updated yesterday; cracked_c5 & isoprene 3 days old; piperylene 5 days old. No staleness beyond a week — the earlier 'stale market data' flag is resolved.", 'cta': 'View market tab →', 'time': '2026-08-28'}]}, {'id': 'chain-value', 'type': 'chain', 'span': 'half', 'title': 'Value Chain — Feedstock → Product Prices', 'nodes': [{'label': '裂解碳五 Cracked C5', 'value': '5531', 'unit': '¥/t', 'delta': '+5.6%', 'delta_tone': 'up', 'note': 'feedstock base', 'note_tone': 'neutral'}, {'label': '裂解碳九 Cracked C9', 'value': '3009', 'unit': '¥/t', 'delta': '+9.7%', 'delta_tone': 'up', 'note': 'feedstock base', 'note_tone': 'neutral'}, {'label': '异戊二烯 Isoprene', 'value': '10620', 'unit': '¥/t', 'delta': '+5.8%', 'delta_tone': 'up', 'note': 'premium product', 'note_tone': 'opp'}, {'label': '双环戊二烯 DCPD', 'value': '6560', 'unit': '¥/t', 'delta': '+17.7%', 'delta_tone': 'up', 'note': 'fastest riser', 'note_tone': 'opp'}, {'label': '间戊二烯 Piperylene', 'value': '6593', 'unit': '¥/t', 'delta': '+3.5%', 'delta_tone': 'up', 'note': 'C5 family', 'note_tone': 'neutral'}, {'label': '戊烷发泡剂 Blowing Agent', 'value': '6195', 'unit': '¥/t', 'delta': '+12.9%', 'delta_tone': 'up', 'note': 'C5 family', 'note_tone': 'neutral'}]}, {'id': 'market-narrative', 'type': 'narrative', 'span': 'half', 'title': 'AI 综合研判 — Market Position', 'body': "The Longzhong market reference prices (erp_sale_order_weighted) show a broad month-over-month rally across the entire C5/C9 value chain, with all six tracked products up between +3.5% (piperylene) and +17.7% (DCPD). The premium isoprene product holds the highest absolute price at ¥10,620/t (+5.8% MoM), confirming its strategic margin position. Rising feedstock costs (cracked C5 +5.6%, cracked C9 +9.7%) will pressure downstream margins unless realized prices keep pace. With record July revenue (¥311M) and a strong August MTD, the business is well-positioned to capture the up-cycle — but should protect premium isoprene capacity and monitor DCPD's sharp run-up for potential demand pullback. Data freshness is healthy (1-5 days), enabling decision-grade market reads."}], 'header': {'greeting': '早上好 — 今日市场全线走高，DCPD 领涨 +17.7%', 'snapshot': [{'label': '异戊二烯', 'value': '¥10,620', 'delta': '+5.8%', 'delta_tone': 'up'}, {'label': '双环戊二烯', 'value': '¥6,560', 'delta': '+17.7%', 'delta_tone': 'up'}, {'label': '裂解碳五', 'value': '¥5,531', 'delta': '+5.6%', 'delta_tone': 'up'}], 'period': '2026-08-28'}, 'footer': {'sources': '数据来源：ERP (erp_v_sale_orderentry) + Longzhong 市场参考价 (market_prices)'}}

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
