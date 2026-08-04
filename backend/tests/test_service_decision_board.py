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


def test_narrative_direction_from_prices_when_pct_missing():
    """expected_change_pct=None must not default to 'rise'; use forecast vs current."""
    s = build_narrative(
        name_zh="发泡剂", day=7, current_price=6800.0, forecast_base=6000.0,
        expected_change_pct=None, p_rise=None, action="watch", confidence="low",
        directional_accuracy=None, intel_event_count=0, intel_bias="neutral",
        volatility_regime=None,
    )
    assert "跌至" in s          # 6,000 < 6,800 → fall, not rise
    assert "涨至" not in s
    assert "(—)" not in s       # no empty parenthetical
    assert "6,000" in s


def test_narrative_direction_up_from_prices_when_pct_missing():
    s = build_narrative(
        name_zh="发泡剂", day=7, current_price=6800.0, forecast_base=7000.0,
        expected_change_pct=None, p_rise=None, action="watch", confidence="low",
        directional_accuracy=None, intel_event_count=0, intel_bias="neutral",
        volatility_regime=None,
    )
    assert "涨至" in s
    assert "(—)" not in s


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


from app.schemas.market_dashboard import (
    DecisionMeta, DirectionalEdgeMeta, ForecastBaseBatchResponse,
    ForecastOverlayMeta, IntelligenceOverlayMeta, MarketSnapshot,
    PolicyOverlayMeta, ProbabilityOverlayMeta, SkuForecastMeta, TrustTierMeta,
)
from app.services import decision_board_service as dbs


def _snapshot(pid, price):
    return MarketSnapshot(
        id=pid, name=pid, group="midstream", price=price, date="2026-08-03",
    )


def _overlay(pid, action="buy", conf="medium"):
    return ForecastOverlayMeta(
        product_id=pid,
        intelligence=IntelligenceOverlayMeta(active_event_count=3, bias_direction="up"),
        policy=PolicyOverlayMeta(volatility_regime="NORMAL"),
        trust_tier=TrustTierMeta(
            tier="high", badge_color="green",
            badge_label_zh="高可信", badge_label_en="High",
        ),
        probability=[ProbabilityOverlayMeta(horizon=7, p_rise=0.68, expected_change_pct=0.029)],
        directional=[DirectionalEdgeMeta(horizon=7, accuracy=0.72, n_test=40, p_value=0.001, status="edge")],
        decision=[DecisionMeta(horizon=7, action=action, confidence=conf, rationale="r")],
    )


def test_build_decision_board_composes(monkeypatch):
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot(pid, 12100.0)
                       for pid in ("isoprene", "piperylene", "dcpd", "raffinate_c5")],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day,
            base_by_product={pid: 12450.0 for pid in product_ids},
            overlay_by_product={pid: _overlay(pid) for pid in product_ids},
            sku_forecasts_by_product={
                "raffinate_c5": [SkuForecastMeta(
                    material_code="C5R-001", target_id="tgt-001",
                    name="抽余C5 SKU", report_order=1,
                    is_primary=True, forecast_base=8100.0,
                )],
            },
        ),
    )
    resp = dbs.build_decision_board("midstream", day=7)
    assert resp.group == "midstream"
    assert len(resp.products) == 4
    iso = next(p for p in resp.products if p.id == "isoprene")
    assert iso.name_zh == "异戊二烯"
    assert iso.code == "isoprene"
    assert iso.current_price == 12100.0
    assert iso.forecast_base == 12450.0
    assert iso.p_rise == 0.68
    assert iso.action == "buy"
    assert iso.action_label_zh == "建议备货"
    assert iso.trust_label_zh == "高可信"
    assert iso.directional_accuracy == 0.72
    assert "12,100" in iso.narrative_zh
    raf = next(p for p in resp.products if p.id == "raffinate_c5")
    assert raf.skus[0].material_code == "C5R-001"
    assert "异戊二烯" in resp.briefing_zh


def test_build_decision_board_missing_overlay(monkeypatch):
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot("isoprene", None)],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day,
            base_by_product={"isoprene": None},
            overlay_by_product={"isoprene": None},
            sku_forecasts_by_product={},
        ),
    )
    # limit group to a single product via PRODUCT_GROUP_MAP patch
    monkeypatch.setattr(dbs, "PRODUCT_GROUP_MAP", {"midstream": ["isoprene"]})
    resp = dbs.build_decision_board("midstream", day=7)
    p = resp.products[0]
    assert p.action == "watch"
    assert p.action_label_zh == "建议观望"
    assert p.one_liner_zh == "数据不足,建议观望"
    assert "数据不足" in p.narrative_zh
