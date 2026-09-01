"""Template brief renderer — 7-section deterministic fallback (DCPD case)."""
from app.services.forecasting.analyst.evidence_pack import build_pack
from app.services.forecasting.analyst.template_brief import render_template_brief
from tests.test_forecast_analyst_pack import _explanation, _history


def _dcpd_pack():
    return build_pack(
        product_id="dcpd", name_zh="双环戊二烯", day=7,
        history_rows=_history(start=7000.0, step=-0.8),
        upstream_histories={"cracked_c5": _history(start=5800.0, step=-0.5)},
        run_results={"7": {"base": [6660.0, 6650.0], "bull": [6900.0, 6950.0], "bear": [6400.0, 6350.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.09, "naive_mape": 0.11},
        explanation=_explanation(), as_of_month=8,
    )


def test_brief_schema_and_source():
    b = render_template_brief(_dcpd_pack())
    for key in ("market_update_zh", "price_data_zh", "upstream_logic_zh",
                "supply_demand_zh", "forecast_zh", "watch_triggers_zh",
                "risk_zh", "source", "day", "generated_at"):
        assert key in b
    assert b["source"] == "template"
    assert isinstance(b["watch_triggers_zh"], list) and len(b["watch_triggers_zh"]) >= 2


def test_brief_upstream_names_elasticity():
    b = render_template_brief(_dcpd_pack())
    assert "裂解碳五" in b["upstream_logic_zh"]        # upstream named
    assert "弹性" in b["upstream_logic_zh"]            # elasticity mentioned


def test_brief_supply_demand_seasonal():
    b = render_template_brief(_dcpd_pack())
    assert "传统需求旺季" in b["supply_demand_zh"]     # seasonal (dcpd month 8 = +1.5)


def test_brief_forecast_range_and_direction():
    b = render_template_brief(_dcpd_pack())
    # bear–bull range present (the key new feature) — last points: bear=6350, bull=6950
    assert "6,350" in b["forecast_zh"]
    assert "6,950" in b["forecast_zh"]
    # directional arrow present
    assert any(arrow in b["forecast_zh"] for arrow in ("↘", "↗", "→", "↔"))


def test_brief_forecast_explains_watch_action():
    b = render_template_brief(_dcpd_pack())
    assert "观望" in b["forecast_zh"]
    assert "历史数据不足" in b["forecast_zh"]    # sparse_data reason in words
    assert "55%" in b["forecast_zh"]            # edge threshold referenced


def test_brief_watch_triggers_actionable():
    b = render_template_brief(_dcpd_pack())
    joined = "。".join(b["watch_triggers_zh"])
    assert "55%" in joined                        # accuracy recovery trigger
    assert "裂解碳五" in joined or "上游" in joined  # upstream stabilization trigger


def test_brief_zero_intel_risk_note():
    b = render_template_brief(_dcpd_pack())
    assert "近期无重大市场事件" in b["risk_zh"]


def test_brief_buy_case():
    exp = _explanation()
    exp["probability"] = {"7": {"p_rise": 0.78, "expected_change_pct": 0.045}}
    exp["directional"] = {"7": {"accuracy": 0.68, "status": "edge", "n_test": 60}}
    exp["decision"] = {"7": {"action": "buy", "confidence": "medium", "rationale": "r"}}
    exp["trust_tier"] = {"tier": "high", "reason_zh": "模型持续跑赢基准",
                          "reason_codes": ["model_skill_high"], "badge_label_zh": "高置信"}
    pack = build_pack(
        product_id="isoprene", name_zh="异戊二烯", day=7,
        history_rows=_history(start=12000.0, step=2.0),
        upstream_histories={"cracked_c5": _history(start=5800.0, step=1.0)},
        run_results={"7": {"base": [12500.0, 12600.0], "bull": [12900.0, 13100.0], "bear": [12100.0, 12000.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.07, "naive_mape": 0.1},
        explanation=exp, as_of_month=8,
    )
    b = render_template_brief(pack)
    assert "备货" in b["forecast_zh"]
    assert "70%" in b["forecast_zh"]            # buy probability threshold


def test_brief_missing_data_degrades():
    pack = build_pack(
        product_id="dcpd", name_zh="双环戊二烯", day=7,
        history_rows=[], upstream_histories={},
        run_results={}, model_detail=None, explanation={}, as_of_month=8,
    )
    b = render_template_brief(pack)
    assert "数据不足" in b["forecast_zh"]
    assert b["upstream_logic_zh"] == ""


def _upstream_pack(pid, name_zh, upstream_histories):
    """An upstream product pack (naphtha / crude_oil) with a valid run."""
    return build_pack(
        product_id=pid, name_zh=name_zh, day=7,
        history_rows=_history(start=6500.0, step=1.5),
        upstream_histories=upstream_histories,
        run_results={"7": {"base": [6700.0, 6750.0], "bull": [7000.0, 7100.0], "bear": [6450.0, 6400.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.08, "naive_mape": 0.1},
        explanation=_explanation(), as_of_month=8,
    )


def test_brief_upstream_uses_macro_supply_demand():
    # naphtha has a parent (crude_oil) but is upstream → macro supply/demand,
    # NOT the ERP-demand seasonal section.
    b = render_template_brief(_upstream_pack(
        "naphtha", "石脑油", {"crude_oil": _history(start=5000.0, step=1.0)}))
    assert b["supply_demand_zh"].startswith("宏观供需")
    assert "传统需求旺季" not in b["supply_demand_zh"]


def test_brief_crude_oil_uses_macro_context():
    # crude_oil = top of chain (empty upstream) → macro context, not empty string.
    b = render_template_brief(_upstream_pack("crude_oil", "原油", {}))
    assert b["upstream_logic_zh"].startswith("上游传导")
    assert "产业链最上游" in b["upstream_logic_zh"]
    assert b["supply_demand_zh"].startswith("宏观供需")


def test_brief_downstream_keeps_erp_supply_demand():
    # downstream (c5_resin) still uses the seasonal ERP-demand section, not the
    # upstream macro section. (c5_resin has a month-8 seasonal rule → 旺季.)
    b = render_template_brief(build_pack(
        product_id="c5_resin", name_zh="C5石油树脂", day=7,
        history_rows=_history(start=12000.0, step=2.0),
        upstream_histories={"cracked_c5": _history(start=5800.0, step=1.0)},
        run_results={"7": {"base": [12500.0, 12600.0], "bull": [12900.0, 13100.0], "bear": [12100.0, 12000.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.07, "naive_mape": 0.1},
        explanation=_explanation(), as_of_month=8,
    ))
    assert b["supply_demand_zh"].startswith("供需研判")
    assert "传统需求旺季" in b["supply_demand_zh"]
