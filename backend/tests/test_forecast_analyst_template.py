"""Template brief renderer — the deterministic fallback (DCPD case)."""
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
    for key in ("summary_zh", "why_price_zh", "why_action_zh",
                "watch_triggers_zh", "risk_zh", "source", "day", "generated_at"):
        assert key in b
    assert b["source"] == "template"
    assert isinstance(b["watch_triggers_zh"], list) and len(b["watch_triggers_zh"]) >= 2


def test_brief_explains_why_price():
    b = render_template_brief(_dcpd_pack())
    assert "裂解碳五" in b["why_price_zh"]        # upstream named
    assert "弹性" in b["why_price_zh"]            # elasticity mentioned
    assert "传统需求旺季" in b["why_price_zh"]     # seasonal (dcpd month 8 = +1.5)


def test_brief_explains_why_action_low_trust():
    b = render_template_brief(_dcpd_pack())
    assert "观望" in b["why_action_zh"]
    assert "历史数据不足" in b["why_action_zh"]    # sparse_data reason in words
    assert "55%" in b["why_action_zh"]            # edge threshold referenced


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
    assert "备货" in b["why_action_zh"]
    assert "70%" in b["why_action_zh"]            # buy probability threshold


def test_brief_missing_data_degrades():
    pack = build_pack(
        product_id="dcpd", name_zh="双环戊二烯", day=7,
        history_rows=[], upstream_histories={},
        run_results={}, model_detail=None, explanation={}, as_of_month=8,
    )
    b = render_template_brief(pack)
    assert "数据不足" in b["summary_zh"]
    assert b["why_price_zh"] == "" or "暂无" in b["why_price_zh"]
