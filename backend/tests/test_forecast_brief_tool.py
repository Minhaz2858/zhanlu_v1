"""forecast_brief chat tool."""
import asyncio

from app.services.tool_handlers import forecast_tool
from app.services.forecasting.analyst import service as analyst_service


def test_forecast_brief_success(monkeypatch):
    brief = {"market_update_zh": "材料B市场情报中性。",
             "price_data_zh": "现价 6,940 元/吨。",
             "upstream_logic_zh": "上游下跌。", "supply_demand_zh": "供需平稳。",
             "forecast_zh": "预计走弱。",
             "watch_triggers_zh": ["t1"], "risk_zh": "", "source": "template", "day": 7}
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: brief)
    out = asyncio.run(forecast_tool._forecast_brief(
        {"product_id": "product_b", "day": 7}, db=None, user_id=None, context=None))
    assert out["success"] is True
    assert out["brief"]["market_update_zh"].startswith("材料B")
    assert out["brief"]["source"] == "template"


def test_forecast_brief_requires_product_id():
    out = asyncio.run(forecast_tool._forecast_brief(
        {}, db=None, user_id=None, context=None))
    assert out["success"] is False


def test_forecast_brief_no_data(monkeypatch):
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: None)
    out = asyncio.run(forecast_tool._forecast_brief(
        {"product_id": "product_b"}, db=None, user_id=None, context=None))
    assert out["success"] is False
    assert "product_b" in out["error"]


def test_forecast_brief_registered():
    from app.services.tool_registry import registry
    assert registry.get_handler("forecast_brief") is not None
