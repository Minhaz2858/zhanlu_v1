"""Decision-center info architecture spec validation (2026-08-29).

The agent can now express the Ecisco CEO Command Center information
presentation: multi-page tabs (`pages`), typed AI-analysis panels
(`panels`: alerts / decisions / narrative / chain / customers / inventory /
competitors / news), executive header (greeting + market snapshot) and
provenance footer. These tests pin the validation contract so a malformed
spec fails fast instead of rendering empty cards.
"""
import pytest
from pydantic import ValidationError

from app.services.tool_handlers.dashboard_tools import (
    CreateFullstackDashboardArgs,
    UpdateFullstackDashboardArgs,
    ALLOWED_PANEL_TYPES,
)


def _base(extra=None):
    spec = {
        "name": "CEO Command Center",
        "slug": "ceo-command-center",
        "datasource_id": "kb_1",
        "metrics": [
            {"id": "rev", "type": "kpi", "title": "Revenue", "sql": "SELECT 1 AS n", "options": {}},
            {"id": "rev_trend", "type": "sparkline", "title": "Rev Trend",
             "sql": "SELECT d AS label, v AS value FROM t", "options": {"pill": "上调", "pill_tone": "up", "confidence": 82}},
        ],
    }
    if extra:
        spec.update(extra)
    return spec


def test_valid_decision_center_spec():
    spec = _base({
        "pages": [{"id": "overview", "label": "CEO 总览"}, {"id": "weekly", "label": "周报行情"}],
        "panels": [
            {"id": "alerts-1", "type": "alerts", "page": "overview", "span": "full",
             "items": [{"severity": "crit", "icon": "⚠", "title": "IP 报价超市场 3.1%",
                        "body": "数据 → 原因 → 建议", "cta": "批准调价 →", "time": "2 h前"}]},
            {"id": "decs-1", "type": "decisions", "page": "overview",
             "items": [{"tag": "高 · 定价调整", "tag_tone": "down", "title": "异戊二烯 下调 ¥200/t",
                        "action": "→ 调至 ¥9,200", "action_tone": "down", "body": "理由",
                        "pnl": "维持现价损失风险：−¥8,000/周", "pnl_tone": "down",
                        "buttons": ["✓ 批准", "调整", "延期"]}]},
            {"id": "chain-1", "type": "chain", "page": "overview",
             "nodes": [{"label": "布伦特", "value": "$79.4", "delta": "↓$1.2", "delta_tone": "down"},
                       {"label": "C5 成本", "value": "¥4,650", "note": "毛利↑扩大", "note_tone": "up"}]},
            {"id": "cust-1", "type": "customers", "page": "overview",
             "rows": [{"avatar": "壳牌", "name": "壳牌化工", "sub": "上次下单：3周前", "revenue": "¥980k/月",
                       "status": "沉默 ⚠", "status_tone": "down"}]},
            {"id": "inv-1", "type": "inventory", "page": "overview",
             "rows": [{"label": "裂解C5", "weeks": 6.4, "max": 8, "tone": "up"},
                      {"label": "双环戊二烯", "weeks": 1.8, "max": 8, "tone": "down", "status": "紧急"}]},
            {"id": "comp-1", "type": "competitors", "page": "weekly",
             "rows": [{"name": "异戊二烯", "our_price": 9400, "lo": 8700, "hi": 10000,
                       "comps": [{"name": "上海石化", "price": 9000}], "diff": 3.1, "diff_tone": "down"}]},
            {"id": "news-1", "type": "news", "page": "weekly",
             "rows": [{"time": "今日", "badge": "上调", "badge_tone": "up", "text": "德荣 SIS 上调 ¥300"}]},
            {"id": "nar-1", "type": "narrative", "page": "overview",
             "title": "SIS 综合研判", "body": "长文分析。"},
        ],
        "header": {"greeting": "早上好，刘总 — 今日有 3 项决策等待批准",
                   "snapshot": [{"label": "布伦特", "value": "$79.4", "delta": "↓$1.2", "delta_tone": "down"}],
                   "period": "W-2025-23"},
        "footer": {"sources": "数据来源：ERP + 市场数据"},
        "layout": [
            {"title": "KPI 总览", "widgets": ["rev"], "page": "overview"},
            {"title": "信号与决策", "widgets": ["rev_trend"], "panels": ["alerts-1", "decs-1"], "page": "overview"},
        ],
    })
    parsed = CreateFullstackDashboardArgs.model_validate(spec)
    assert parsed.pages[0]["label"] == "CEO 总览"
    assert len(parsed.panels) == 8
    assert parsed.header["greeting"].startswith("早上好")
    assert parsed.footer["sources"].startswith("数据来源")
    # sparkline is now a first-class widget type
    assert parsed.metrics[1].type == "sparkline"
    # layout passes through untouched (page + panels keys)
    assert parsed.layout[1]["panels"] == ["alerts-1", "decs-1"]


def test_all_panel_types_registered():
    assert ALLOWED_PANEL_TYPES == {
        "alerts", "decisions", "narrative", "chain",
        "customers", "inventory", "competitors", "news",
    }


def test_unknown_panel_type_rejected():
    with pytest.raises(ValidationError, match="panels\\[\\].type must be one of"):
        CreateFullstackDashboardArgs.model_validate(_base({
            "panels": [{"id": "x", "type": "magic", "items": [{"title": "t"}]}],
        }))


def test_alerts_panel_requires_items():
    with pytest.raises(ValidationError, match="requires a non-empty items/rows list"):
        CreateFullstackDashboardArgs.model_validate(_base({
            "panels": [{"id": "x", "type": "alerts", "items": []}],
        }))


def test_chain_panel_requires_nodes():
    with pytest.raises(ValidationError, match="requires a non-empty nodes list"):
        CreateFullstackDashboardArgs.model_validate(_base({
            "panels": [{"id": "x", "type": "chain"}],
        }))


def test_narrative_panel_requires_body():
    with pytest.raises(ValidationError, match="requires a body"):
        CreateFullstackDashboardArgs.model_validate(_base({
            "panels": [{"id": "x", "type": "narrative", "title": "no body"}],
        }))


def test_pages_require_id_and_label():
    with pytest.raises(ValidationError, match="require both 'id' and 'label'"):
        CreateFullstackDashboardArgs.model_validate(_base({"pages": [{"id": "x"}]}))


def test_update_args_accept_new_fields():
    parsed = UpdateFullstackDashboardArgs.model_validate({
        "slug": "ceo-command-center",
        "pages": [{"id": "overview", "label": "CEO 总览"}],
        "panels": [{"id": "a", "type": "alerts", "items": [{"severity": "warn", "title": "t"}]}],
        "header": {"period": "W-2025-23"},
        "footer": {"sources": "来源"},
    })
    assert parsed.pages[0]["id"] == "overview"
    assert parsed.footer["sources"] == "来源"


def test_update_args_reject_bad_panels():
    with pytest.raises(ValidationError, match="must be one of"):
        UpdateFullstackDashboardArgs.model_validate({
            "slug": "ceo-command-center",
            "panels": [{"id": "a", "type": "nope"}],
        })
