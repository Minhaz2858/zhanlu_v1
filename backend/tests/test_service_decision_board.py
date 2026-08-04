"""decision_board_service — pure narrative builders (no I/O)."""
from app.services.decision_board_service import (
    action_label, build_briefing, build_narrative, build_one_liner,
)
from app.schemas.market_dashboard import DecisionBoardProduct


def test_action_label_mapping():
    assert action_label("buy") == "建议备货"
    assert action_label("sell") == "建议出货"
    assert action_label("hold") == "按需跟进"
    assert action_label("watch") == "建议观望"
    assert action_label(None) == "建议观望"
    assert action_label("BUY") == "建议备货"


def test_one_liner_full_data():
    s = build_one_liner(
        action="buy", p_rise=0.68, expected_change_pct=0.029,
        intel_event_count=3, intel_bias="up",
    )
    assert "+2.9%" in s
    assert "68%" in s
    assert "建议备货" in s
    assert "3 条" in s


def test_one_liner_missing_data():
    assert build_one_liner(
        action="watch", p_rise=None, expected_change_pct=None,
    ) == "数据不足,建议观望"


def test_narrative_full_data():
    s = build_narrative(
        name_zh="异戊二烯", day=7, current_price=12100.0, forecast_base=12450.0,
        expected_change_pct=0.029, p_rise=0.68, action="buy", confidence="medium",
        directional_accuracy=0.72, intel_event_count=3, intel_bias="up",
        volatility_regime="NORMAL",
    )
    assert "异戊二烯" in s
    assert "12,100" in s
    assert "12,450" in s
    assert "+2.9%" in s
    assert "68%" in s
    assert "72%" in s          # directional accuracy
    assert "提前备货" in s
    assert "置信度:中" in s


def test_narrative_missing_forecast():
    s = build_narrative(
        name_zh="抽余C5", day=7, current_price=None, forecast_base=None,
        expected_change_pct=None, p_rise=None, action="watch", confidence="low",
        directional_accuracy=None, intel_event_count=0, intel_bias="neutral",
        volatility_regime=None,
    )
    assert "数据不足" in s
    assert "观望" in s


def test_briefing_with_strong_signal():
    products = [
        DecisionBoardProduct(
            id="isoprene", name_zh="异戊二烯", name_en="Isoprene", code="isoprene",
            action="buy", confidence="medium", p_rise=0.68,
        ),
        DecisionBoardProduct(
            id="dcpd", name_zh="双环戊二烯", name_en="DCPD", code="dcpd",
            action="watch", confidence="low",
        ),
    ]
    s = build_briefing(products, 7)
    assert "异戊二烯" in s
    assert "看涨" in s
    assert "双环戊二烯" in s   # mentioned in the tail


def test_briefing_all_quiet():
    products = [
        DecisionBoardProduct(
            id="isoprene", name_zh="异戊二烯", name_en="Isoprene", code="isoprene",
            action="watch", confidence="low",
        ),
    ]
    s = build_briefing(products, 7)
    assert "平稳" in s
