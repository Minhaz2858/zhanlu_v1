"""Weekly report — analyst brief replaces the placeholder narrative."""
from app.services.weekly_report import generator as wrg
from app.services.forecasting.analyst import service as analyst_service


def test_section_uses_analyst_brief(monkeypatch):
    monkeypatch.setattr(wrg, "collect_all_numbers",
                        lambda pid, db: {"current_price": 6940.0, "change_7d_pct": -1.2})
    monkeypatch.setattr(wrg, "render_chart_stub", lambda *a, **k: None)
    brief = {"market_update_zh": "市场动态:双环戊二烯市场平稳。",
             "price_data_zh": "价格数据:现价 6,940 元/吨。",
             "upstream_logic_zh": "上游裂解碳五下跌,成本端下移。",
             "supply_demand_zh": "供需研判:按需采购。",
             "forecast_zh": "价格预测:预计下周走弱。",
             "watch_triggers_zh": [], "risk_zh": "",
             "source": "template", "day": 7}
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: brief)
    svc = wrg.WeeklyReportService(db=None)
    section = svc._build_product_section("dcpd", "2026-08-03", "2026-08-09", {}, False, False)
    assert "6,940" in section["narrative"]
    assert "成本端下移" in section["narrative"]


def test_section_falls_back_to_placeholder(monkeypatch):
    monkeypatch.setattr(wrg, "collect_all_numbers",
                        lambda pid, db: {"current_price": None})
    monkeypatch.setattr(wrg, "render_chart_stub", lambda *a, **k: None)
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: None)
    svc = wrg.WeeklyReportService(db=None)
    section = svc._build_product_section("dcpd", "2026-08-03", "2026-08-09", {}, False, False)
    assert "市场运行平稳" in section["narrative"]  # legacy placeholder
