"""Generated dashboard app router for `ceo-decision-center-demo`. DO NOT EDIT.

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

SLUG = 'ceo-decision-center-demo'
CONFIG = {'name': 'CEO 经营决策中心', 'slug': 'ceo-decision-center-demo', 'description': '经营决策中心 — 订单 · 毛利 · 客户 · 区域（数据：demo_e2e）', 'theme': 'dark', 'style': 'ceo', 'refresh_interval_seconds': 60, 'design_system_ref': None, 'design': {}, 'metrics': [{'id': 'kpi_rev', 'title': '客户总收入', 'type': 'kpi', 'options': {'unit': '¥', 'accent': '#22C55E', 'sub': '8 客户 · 4 区域'}}, {'id': 'kpi_qty', 'title': '总销量', 'type': 'kpi', 'options': {'unit': '件', 'accent': '#3B82F6', 'sub': '16 订单 · 平均 50 件/单'}}, {'id': 'kpi_margin', 'title': '综合毛利率', 'type': 'kpi', 'options': {'unit': '%', 'accent': '#F59E0B', 'delta_tone': 'warn', 'sub': '目标 16% · 底线 10%'}}, {'id': 'kpi_orders', 'title': '订单数', 'type': 'kpi', 'options': {'unit': '单', 'accent': '#8B5CF6', 'sub': '8 产品 · 8 客户'}}, {'id': 'kpi_top', 'title': '最大客户 Acme', 'type': 'kpi', 'options': {'unit': '¥', 'accent': '#14B8A6', 'sub': '占客户收入 19.4%'}}, {'id': 'signal_table', 'title': '产品信号一览', 'type': 'table', 'options': {'pills': {'column': 'action', 'map': {'上调': 'up', '下调': 'down', '关注': 'warn'}}, 'row_tone_column': 'tone', 'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 21.2}}}, {'id': 'prod_qty', 'title': '产品销量 (件)', 'type': 'bar', 'options': {'topItem': {'label': 'Widget A', 'value': 170.0, 'share_pct': 21.2}}}, {'id': 'prod_margin', 'title': '产品毛利率 (%)', 'type': 'bar', 'options': {'topItem': {'label': 'Gizmo D', 'value': 17.0, 'share_pct': 15.4}}}, {'id': 'region_qty', 'title': '区域销量 (件)', 'type': 'bar', 'options': {'topItem': {'label': 'North', 'value': 428.0, 'share_pct': 53.3}}}, {'id': 'region_pie', 'title': '区域销量占比', 'type': 'pie', 'options': {'topItem': {'label': 'North', 'value': 428.0, 'share_pct': 53.3}}}, {'id': 'customer_rev', 'title': '客户收入 (¥)', 'type': 'bar', 'options': {'topItem': {'label': 'Acme Corp', 'value': 1250000.0, 'share_pct': 19.5}}}], 'filters': [], 'insights': [{'title': '🏆 Top: Widget A', 'body': 'Widget A leads with 170 (21.2% of the total).'}, {'title': '🏆 Top: Widget A', 'body': 'Widget A leads with 170 (21.2% of the total).'}, {'title': '🏆 Top: Gizmo D', 'body': 'Gizmo D leads with 17 (15.4% of the total).'}], 'layout': [{'title': '经营总览', 'widgets': ['kpi_rev', 'kpi_qty', 'kpi_margin', 'kpi_orders', 'kpi_top'], 'page': 'overview'}, {'title': '今日信号', 'panels': ['alerts-1'], 'page': 'overview'}, {'title': '产品信号与决策', 'widgets': ['signal_table', 'prod_qty'], 'panels': ['decs-1'], 'page': 'overview'}, {'title': '客户与区域', 'widgets': ['region_qty', 'customer_rev', 'region_pie'], 'panels': ['cust-1', 'chain-1', 'vol-1'], 'page': 'overview'}, {'title': '产品详情', 'widgets': ['prod_qty', 'prod_margin', 'signal_table'], 'page': 'products'}, {'title': '区域分析', 'widgets': ['region_qty', 'region_pie', 'customer_rev'], 'page': 'regions'}], 'pages': [{'id': 'overview', 'label': 'CEO 总览'}, {'id': 'products', 'label': '产品详情'}, {'id': 'regions', 'label': '区域分析'}], 'panels': [{'id': 'alerts-1', 'type': 'alerts', 'page': 'overview', 'items': [{'severity': 'crit', 'icon': '⚠', 'title': 'Cog G 毛利率仅 8.5% — 显著低于综合 13.8%', 'body': 'Cog G 两笔订单（30 + 20 件）平均毛利 8.5%，低于综合毛利率 13.8% 达 5.3pp。买家为 Wayne Enterprises（月收入贡献 ¥690k）与 Initech。建议：重新议价或优化成本，目标 ≥ 12%。', 'cta': '审查定价 →', 'time': '本周'}, {'severity': 'crit', 'icon': '📦', 'title': 'South 区域销量 130 件 — 仅占 16.2%', 'body': 'South 由 Initech 与 Vandelay 支撑，销量占比 16.2%，远低于 North 的 53.3%。建议：评估区域销售投入与渠道覆盖。', 'cta': '查看区域 →', 'time': '本周'}, {'severity': 'opp', 'icon': '📈', 'title': 'Widget A 销量 170 件 — 全品类第一', 'body': 'Widget A 占 21.2% 销量且平均毛利 16.0% 高于综合水平，Acme Corp 为主要驱动。建议：维持供应优先级，评估提价空间。', 'cta': '评估提价 →', 'time': '今日'}, {'severity': 'warn', 'icon': '👤', 'title': 'Hooli 订单规模偏小 — 22-25 件/单', 'body': 'Hooli 两笔订单合计 47 件，单均规模低于整体均值。建议：销售团队跟进增购机会。', 'cta': '跟进客户 →', 'time': '今日'}]}, {'id': 'decs-1', 'type': 'decisions', 'page': 'overview', 'items': [{'tag': '高 · 定价调整', 'tag_tone': 'down', 'title': 'Cog G 议价目标 ≥ 12% 毛利', 'action': '→ 当前 8.5% · 需提升 +3.5pp', 'action_tone': 'down', 'body': 'Cog G 毛利 8.5% 低于综合 5.3pp，涉及 Wayne Enterprises（¥690k/月）与 Initech 两买家。建议销售团队本周启动议价。', 'pnl': '毛利缺口 5.3pp · 影响 50 件订单', 'pnl_tone': 'down', 'buttons': ['✓ 启动议价', '延期', '观察']}, {'tag': '中 · 供应优先级', 'tag_tone': 'up', 'title': 'Widget A 供应优先级提升', 'action': '→ 销量第一 · 毛利 16.0%', 'action_tone': 'up', 'body': 'Widget A 贡献 21.2% 销量且毛利高于综合，Acme 为核心客户（¥1.25M）。建议维持优先排产。', 'pnl': '守护 ¥1.25M 客户收入', 'pnl_tone': 'up', 'buttons': ['✓ 确认', '审查']}, {'tag': '低 · 区域跟进', 'tag_tone': 'warn', 'title': 'South 区域销售复盘', 'action': '→ 销量占比 16.2%', 'action_tone': 'warn', 'body': 'South 区域由 Initech + Vandelay 支撑，销量 130 件。建议季度复盘渠道策略。', 'pnl': '潜在增量 20-30 件/季', 'pnl_tone': 'warn', 'buttons': ['✓ 安排复盘', '跳过']}]}, {'id': 'nar-1', 'type': 'narrative', 'page': 'overview', 'title': '本周经营研判', 'body': '全渠道销量 803 件、客户收入 ¥6.43M、综合毛利率 13.8%——整体健康但结构分化明显：头部产品 Widget A / Widget B / Gadget C 合计贡献 56.7% 销量且毛利高于均值，是基本盘；Cog G（8.5%）与 Pin H（15.5% 但仅 47 件）处于毛利或规模短板；区域上 North 一家独大（53.3%），South 与 East 合计仅 25.3%，存在明显增量空间。建议：本周优先落地 Cog G 议价与 South 区域复盘，同时维持 Widget A 供应优先级。'}, {'id': 'chain-1', 'type': 'chain', 'page': 'overview', 'title': '客户价值链', 'nodes': [{'label': '客户总收入', 'value': '¥6.43M', 'note': '8 客户 · 16 订单', 'note_tone': 'up'}, {'label': 'North 区域', 'value': '¥2.92M', 'delta': '45.4% 占比', 'delta_tone': 'up', 'note': '头部集中', 'note_tone': 'warn'}, {'label': 'Acme Corp', 'value': '¥1.25M', 'delta': '19.4% 占比', 'delta_tone': 'up', 'note': '最大客户', 'note_tone': 'up'}, {'label': '毛利最薄', 'value': 'Cog G 8.5%', 'delta': '低于综合 5.3pp', 'delta_tone': 'down', 'note': '需调价/提效', 'note_tone': 'down'}]}, {'id': 'cust-1', 'type': 'customers', 'page': 'overview', 'rows': [{'avatar': 'Acme', 'name': 'Acme Corp', 'sub': '区域 North · 2 订单', 'revenue': '¥1.25M', 'status': '核心 ✓', 'status_tone': 'up'}, {'avatar': 'Glx', 'name': 'Globex', 'sub': '区域 North · 2 订单', 'revenue': '¥980k', 'status': '活跃 ✓', 'status_tone': 'up'}, {'avatar': 'Ini', 'name': 'Initech', 'sub': '区域 South · 2 订单', 'revenue': '¥875k', 'status': '活跃 ✓', 'status_tone': 'up'}, {'avatar': 'Umb', 'name': 'Umbrella', 'sub': '区域 West · 2 订单', 'revenue': '¥760k', 'status': '偏慢', 'status_tone': 'warn'}, {'avatar': 'Stk', 'name': 'Stark Industries', 'sub': '区域 East · 2 订单', 'revenue': '¥720k', 'status': '偏慢', 'status_tone': 'warn'}, {'avatar': 'Way', 'name': 'Wayne Enterprises', 'sub': '区域 North · 2 订单', 'revenue': '¥690k', 'status': '毛利承压', 'status_tone': 'down'}, {'avatar': 'Hoo', 'name': 'Hooli', 'sub': '区域 West · 2 订单', 'revenue': '¥610k', 'status': '订单偏小', 'status_tone': 'down'}, {'avatar': 'Van', 'name': 'Vandelay', 'sub': '区域 South · 1 订单', 'revenue': '¥540k', 'status': '待激活', 'status_tone': 'down'}]}, {'id': 'vol-1', 'type': 'inventory', 'page': 'overview', 'max': 200, 'rows': [{'label': 'Widget A', 'weeks': 170, 'tone': 'up', 'status': '强'}, {'label': 'Widget B', 'weeks': 150, 'tone': 'up', 'status': '强'}, {'label': 'Gadget C', 'weeks': 135, 'tone': 'up', 'status': '强'}, {'label': 'Gizmo D', 'weeks': 93, 'tone': 'warn', 'status': '中'}, {'label': 'Sprocket E', 'weeks': 90, 'tone': 'warn', 'status': '中'}, {'label': 'Flange F', 'weeks': 68, 'tone': 'warn', 'status': '中'}, {'label': 'Cog G', 'weeks': 50, 'tone': 'down', 'status': '弱'}, {'label': 'Pin H', 'weeks': 47, 'tone': 'down', 'status': '弱'}]}], 'header': {'greeting': '经营决策中心 — 今日有 2 项定价决策待审', 'snapshot': [{'label': '总销量', 'value': '803 件', 'delta': '16 订单', 'delta_tone': 'up'}, {'label': '综合毛利率', 'value': '13.8%', 'delta': '目标 16%', 'delta_tone': 'warn'}, {'label': '客户收入', 'value': '¥6.43M', 'delta': '8 客户', 'delta_tone': 'up'}, {'label': '区域覆盖', 'value': '4 区域', 'delta': 'North 53%', 'delta_tone': 'neutral'}], 'period': 'DEMO E2E · 2026-08'}, 'footer': {'sources': '数据来源：demo_e2e (PostgreSQL) · orders / products / customers · 只读 SQL · 由 Zhanlu Agent 生成'}}

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
