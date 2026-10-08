# Forecast Analyst Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Decision Board's AI简评/AI建议 reason like an analyst — richer decision evidence (price-chain transmission, model agreement, seasonal position, machine-readable trust reasons) plus an LLM brief writer grounded in a verified evidence pack — surfaced on the Decision Board, forecast_agent chat, and weekly report.

**Architecture:** New module `backend/app/services/forecasting/analyst/` with pure pack/template/verifier functions and a caching service. Briefs are cached in `ForecastRun.explanation["analyst_brief"][str(day)]` (JSON column, no migration). LLM writes only from the evidence pack; a numeric verifier rejects hallucinations and falls back to an enriched deterministic template. **The LLM explains, never decides** — all buy/hold/sell/watch logic stays deterministic.

**Tech Stack:** FastAPI backend (sync SQLAlchemy), pydantic schemas, `chat_completion_json_sync` LLM helper, React + TanStack Query frontend, pytest (backend venv), vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-08-04-forecast-analyst-design.md`

## Global Constraints

- **Honesty boundary:** decisions (action/confidence) are NEVER produced or modified by the LLM. LLM output is prose only.
- Every number in an LLM brief MUST exist in the evidence pack; verifier rejects otherwise → template fallback.
- All analyst failures are additive-safe: board, forecast run, weekly report must NEVER break because of analyst code (try/except everywhere at integration points).
- Feature flags: `FORECAST_ANALYST_LLM_ENABLED: bool = False`, `FORECAST_DOMAIN_SIGNALS_ENABLED: bool = False` (defaults off; explicit rows in `app/config.py`).
- Narrative language: Simplified Chinese (zh-CN). Chinese market colors: 涨=red, 跌=green.
- **No git commits** — user works on `main` with uncommitted changes and commits themselves. Skip all commit steps.
- Backend tests: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/<file> -v` — run individually (SSH RAM limited).
- Frontend tests: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`.
- After backend service changes: `docker restart zhanlu-backend` then sleep ~20s. Backend port 5002.

## Key Existing Code (verified signatures)

- `ForecastRun.explanation` dict keys: `summary, confidence, drivers[{feature,weight}], coherence_flags, drift_warning, intelligence, policy, domain_signals, trust_tier, probability, directional, decision` (engine.py:629-642).
  - `probability`: `{"7": {"p_rise": float, "p_rise_gt": dict, "expected_change_pct": float}}`
  - `directional`: `{"7": {"accuracy": float|None, "n_test": int, "p_value": float|None, "status": "edge"|"no_edge"}}`
  - `decision`: `{"7": {"action": str, "confidence": str, "rationale": str}}`
  - `trust_tier`: full dict from `compute_forecast_trust_tier()` incl. `tier, reason_zh, badge_color, badge_label_zh`
  - `intelligence`: `{active_event_count, bias_direction, impact_tier, confidence, summary}` or None
  - `policy`: `{volatility_regime, vol_multiplier, bias_pct, diagnosis_bias, applied_factor}` or None
  - `domain_signals`: `{seasonal_pct, causal_pct, total_pct, applied_rules}` or None
- `run.results`: `{str(h): {"base": [...], "bull": [...], "bear": [...]}}`; board uses `base[0]` (batch v2, mds:944-952).
- `run.model_detail`: `{models_run, models_failed, weights, ensemble_mape, naive_mape, metric}`.
- `mds._read_product_history_rows(product_id)` → `[(date_str, price), ...]` ascending; used by `get_forecast_analysis` (mds:1902).
- `mds.PRODUCT_FORECAST_TARGET_KEY`: product_id → `"acme.<pid>"` (mds:859-873).
- `PRODUCT_LABELS.get(pid, {}).get("label_zh", pid)` for Chinese names.
- `forecasting/narrative.py`: `compute_trend_stats(history_rows)`, `compute_uncertainty_stats(horizon_payload)`, `compute_model_stats(model_detail, below_naive)` — reused by the pack.
- `forecasting/domain_signals.py`: `compute_seasonal_adjustment(product_id, month)`, `compute_causal_chain_adjustment(product_id, product_c_pct)`, private `_ELASTICITIES`.
- `forecasting/engine.py`: `forecasts` dict (name → pd.Series, len h_max) at :268; explanation assembly at :629; run flush at :732; `_get_config_bool(key, default)` at :1014.
- `llm_service.chat_completion_json_sync(prompt, schema, temperature=0.0) -> dict` (returns `{}` on any error; sync; 30s timeout).
- `decision_board_service.build_decision_board(group, day, db)` — per-product loop at :192-268; `build_narrative(...)` at :85.
- Tool pattern: `tool_handlers/forecast_tool.py` — async handler + `SCHEMA` dict + `registry.register(...)` tuple at :906-971.
- `weekly_report/generator.py:_build_product_section` (:154-183) — narrative placeholder at :169-173.
- `app/config.py` forecast flags at :381-390.

---

### Task 1: Trust-tier machine-readable reason codes

**Files:**
- Modify: `backend/app/services/forecasting/forecast_trust_tier.py`
- Test: `backend/tests/test_forecast_trust_reasons.py`

**Interfaces:**
- Produces: `compute_forecast_trust_tier(...)` output dict gains key `reason_codes: list[str]` — consumed by Task 3 pack builder. Codes: `"sparse_data"`, `"below_naive_baseline"`, `"weekly_cadence"`, `"model_skill_high"`, `"model_skill_medium"`.

- [ ] **Step 1: Write the failing test**

```python
"""forecast_trust_tier — machine-readable reason codes."""
from app.services.forecasting.forecast_trust_tier import compute_forecast_trust_tier


def test_low_tier_sparse_data_code():
    out = compute_forecast_trust_tier("product_b", below_naive=False, cadence_class="sparse")
    assert out["tier"] == "low"
    assert "sparse_data" in out["reason_codes"]


def test_directional_below_naive_code():
    out = compute_forecast_trust_tier("product_d", below_naive=True, cadence_class="daily")
    assert out["tier"] == "directional"
    assert "below_naive_baseline" in out["reason_codes"]


def test_directional_weekly_product_has_both_codes():
    out = compute_forecast_trust_tier("cracked_c9", below_naive=True, cadence_class="weekly")
    assert "below_naive_baseline" in out["reason_codes"]
    assert "weekly_cadence" in out["reason_codes"]


def test_high_tier_code():
    out = compute_forecast_trust_tier("product_d", below_naive=False, cadence_class="daily", mape=8.0)
    assert out["tier"] == "high"
    assert out["reason_codes"] == ["model_skill_high"]


def test_medium_weekly_code():
    out = compute_forecast_trust_tier("feedstock_a", below_naive=False, cadence_class="weekly")
    assert out["tier"] == "medium"
    assert "weekly_cadence" in out["reason_codes"]


def test_medium_generic_code():
    out = compute_forecast_trust_tier("styrene", below_naive=False, cadence_class="daily")
    assert out["tier"] == "medium"
    assert out["reason_codes"] == ["model_skill_medium"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_trust_reasons.py -v`
Expected: FAIL — `KeyError: 'reason_codes'`

- [ ] **Step 3: Implement reason codes**

In `compute_forecast_trust_tier`, add a `reason_codes` list built alongside each branch:

```python
    if cadence_class in ("sparse", "unsupported", "unknown"):
        tier = "low"
        reason_codes = ["sparse_data"]
        # ... existing badge/reason assignments unchanged
    elif below_naive:
        tier = "directional"
        reason_codes = ["below_naive_baseline"]
        if product_id in _WEEKLY_CADENCE_PRODUCTS:
            reason_codes.append("weekly_cadence")
        # ...
    elif cadence_class == "weekly" and product_id not in _HIGH_SKILL_PRODUCTS:
        tier = "medium"
        reason_codes = ["weekly_cadence"]
        # ...
    elif product_id in _HIGH_SKILL_PRODUCTS and not below_naive:
        tier = "high"
        reason_codes = ["model_skill_high"]
        # ...
    else:
        tier = "medium"
        reason_codes = ["model_skill_medium"]
```

Add `"reason_codes": reason_codes` to the returned dict (after `"reason_en"`).

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_trust_reasons.py tests/test_forecast_trust_tier.py -v` (second file if it exists)
Expected: PASS

---

### Task 2: Elasticity accessor + engine-time model agreement metric

**Files:**
- Modify: `backend/app/services/forecasting/domain_signals.py` (add `get_elasticity`)
- Create: `backend/app/services/forecasting/analyst/__init__.py`
- Create: `backend/app/services/forecasting/analyst/evidence_pack.py` (pure helpers only in this task)
- Modify: `backend/app/services/forecasting/engine.py:574` (inside Step 9 try block, before `explanation_dict = {...}` at :629)
- Test: `backend/tests/test_forecast_analyst_agreement.py`

**Interfaces:**
- Produces: `get_elasticity(product_id: str) -> float | None` (e.g. product_b → 0.663).
- Produces: `compute_model_agreement(forecasts: dict[str, pd.Series], horizon: int) -> dict | None` returning `{"n_models": int, "min": float, "max": float, "spread_pct": float}` (spread = (max−min)/mean of model endpoints), None when <2 usable models.
- Produces: `explanation_dict["model_agreement"]` = `{str(h): {...}}` — consumed by Task 3 pack.

- [ ] **Step 1: Write the failing test**

```python
"""Model agreement metric + elasticity accessor."""
import pandas as pd

from app.services.forecasting.analyst.evidence_pack import compute_model_agreement
from app.services.forecasting.domain_signals import get_elasticity


def test_get_elasticity_product_b():
    assert get_elasticity("product_b") == 0.663


def test_get_elasticity_unknown():
    assert get_elasticity("not_a_product") is None


def test_agreement_tight_cluster():
    fc = {
        "arima": pd.Series([100.0, 101.0]),
        "ets": pd.Series([100.5, 101.5]),
        "stl": pd.Series([99.5, 100.8]),
    }
    out = compute_model_agreement(fc, 2)
    assert out["n_models"] == 3
    assert out["min"] == 100.8
    assert out["max"] == 101.5
    assert out["spread_pct"] < 0.01


def test_agreement_wide_spread():
    fc = {"a": pd.Series([100.0]), "b": pd.Series([120.0])}
    out = compute_model_agreement(fc, 1)
    assert out["spread_pct"] == round(20.0 / 110.0, 4)


def test_agreement_single_model_returns_none():
    assert compute_model_agreement({"a": pd.Series([100.0])}, 1) is None


def test_agreement_short_series_skipped():
    fc = {"a": pd.Series([100.0, 101.0]), "b": pd.Series([50.0])}  # b too short for h=2
    assert compute_model_agreement(fc, 2) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_agreement.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.forecasting.analyst`

- [ ] **Step 3: Implement**

`backend/app/services/forecasting/analyst/__init__.py`:
```python
"""Forecast analyst — evidence pack, LLM brief writer, cached briefs."""
```

Add to `domain_signals.py` (after `_ELASTICITIES` definition, :64):
```python
def get_elasticity(product_id: str) -> float | None:
    """Public accessor for the effective chain elasticity of a product."""
    return _ELASTICITIES.get(product_id)
```

Create `evidence_pack.py` with the agreement helper:
```python
"""Evidence pack assembly for the forecast analyst (pure builders)."""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd


def compute_model_agreement(forecasts: dict, horizon: int) -> Optional[dict]:
    """Spread across per-model point forecasts at `horizon` (endpoint)."""
    endpoints: list[float] = []
    for _name, fc in (forecasts or {}).items():
        try:
            if fc is not None and len(fc) >= horizon:
                v = float(fc.iloc[horizon - 1])
                if math.isfinite(v):
                    endpoints.append(v)
        except Exception:
            continue
    if len(endpoints) < 2:
        return None
    mean_v = sum(endpoints) / len(endpoints)
    return {
        "n_models": len(endpoints),
        "min": round(min(endpoints), 2),
        "max": round(max(endpoints), 2),
        "spread_pct": round((max(endpoints) - min(endpoints)) / mean_v, 4) if mean_v else None,
    }
```

In `engine.py`, inside the Step 9 try block after the decision fill loop (after :627, before `explanation_dict = {`), add:
```python
            # --- Model agreement metric (analyst evidence) ---
            from app.services.forecasting.analyst.evidence_pack import (
                compute_model_agreement,
            )
            model_agreement_report: dict = {}
            for h in horizons:
                h_int = int(h)
                ag = compute_model_agreement(forecasts, h_int)
                if ag is not None:
                    model_agreement_report[str(h_int)] = ag
```
and add `"model_agreement": model_agreement_report,` to `explanation_dict` (after `"decision": decision_report,`).

Note: the agreement block must also exist when the outer try fails early — add `model_agreement_report: dict = {}` next to `explanation_dict: dict | None = None` (:513) is NOT needed since it's scoped inside the try; but if `explanation_dict` stays None on exception, nothing references the report. Keep it inside the try.

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_agreement.py -v`
Expected: PASS

---

### Task 3: Evidence pack builder

**Files:**
- Modify: `backend/app/services/market_dashboard_service.py` — add public alias after `_read_product_history_rows` definition
- Modify: `backend/app/services/forecasting/analyst/evidence_pack.py`
- Test: `backend/tests/test_forecast_analyst_pack.py`

**Interfaces:**
- Consumes: Task 1 `reason_codes`, Task 2 `compute_model_agreement` output shape, `narrative.compute_trend_stats/compute_uncertainty_stats/compute_model_stats`, `domain_signals.get_elasticity/compute_seasonal_adjustment`.
- Produces: `UPSTREAM_MAP: dict[str, list[str]]`
- Produces: `build_pack(*, product_id, name_zh, day, history_rows, upstream_histories, run_results, model_detail, explanation, as_of_month) -> dict` — the full evidence pack consumed by Tasks 4/5/6.
- Produces: `compute_price_percentile(history_rows) -> float | None` (percent of history below last price, 1 decimal; None if <10 points).

Pack shape (all numbers raw, not formatted):
```python
{
  "product_id": str, "name_zh": str, "day": int,
  "current_price": float|None, "price_date": str|None,
  "price_percentile": float|None,               # 0-100, 3yr
  "forecast_base": float|None, "forecast_end": float|None,
  "bull": float|None, "bear": float|None, "spread_pct": float|None,
  "expected_change_pct": float|None,            # fraction
  "p_rise": float|None,
  "trend": {"chg_7d_pct": …, "chg_30d_pct": …, "ma30": …, "above_ma30": …},
  "models": {"model_count": int, "ensemble_mape": …, "naive_mape": …,
             "beats_naive": …, "below_naive_baseline": …,
             "agreement": {"n_models","min","max","spread_pct"} | None},
  "trust": {"tier": str|None, "reason_zh": str|None, "reason_codes": list},
  "directional": {"accuracy": …, "status": …, "n_test": …},
  "decision": {"action": str, "confidence": str, "rationale": str|None},
  "drivers": [{"feature": str, "weight": float}],   # top 3
  "upstream": [{"product_id","name_zh","chg_30d_pct","percentile"}],
  "causal": {"elasticity": float|None, "implied_pct": float|None,
             "divergent": bool},
  "seasonal": {"month": int, "adj_pct": float, "label_zh": str|None},
  "intelligence": {"event_count": int, "bias": str, "summary": str|None},
  "policy": {"volatility_regime": str|None},
}
```
`causal.implied_pct` = upstream[0].chg_30d_pct × elasticity (percent units). `divergent` = implied and expected_change_pct×100 differ in sign AND |implied − expected×100| > 2.0.
`seasonal.label_zh`: adj<0 → "传统需求淡季", adj>0 → "传统需求旺季", 0 → None.
`UPSTREAM_MAP`: product_b/product_d/product_f/feedstock_a/sis/product_a → ["cracked_c5"]; styrene/blowing_agent/cracked_c9 → wait, cracked_c9's upstream is product_c: styrene/blowing_agent → ["cracked_c9"]; cracked_c5/cracked_c9 → ["product_c"]; product_c → ["product_e"]; product_e → [].

- [ ] **Step 1: Write the failing test**

```python
"""Evidence pack builder — pure assembly."""
from app.services.forecasting.analyst.evidence_pack import (
    UPSTREAM_MAP, build_pack, compute_price_percentile,
)


def _history(start=6000.0, n=100, step=1.0):
    return [(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", start + i * step) for i in range(n)]


def _explanation():
    return {
        "drivers": [{"feature": "cracked_c5", "weight": 0.42},
                     {"feature": "fx", "weight": 0.2},
                     {"feature": "event", "weight": 0.1},
                     {"feature": "extra", "weight": 0.05}],
        "trust_tier": {"tier": "low", "reason_zh": "历史数据稀疏",
                        "reason_codes": ["sparse_data"], "badge_label_zh": "数据不足"},
        "probability": {"7": {"p_rise": 0.42, "expected_change_pct": -0.04}},
        "directional": {"7": {"accuracy": 0.51, "status": "no_edge", "n_test": 40}},
        "decision": {"7": {"action": "watch", "confidence": "low", "rationale": "Low trust"}},
        "model_agreement": {"7": {"n_models": 8, "min": 6580.0, "max": 6790.0, "spread_pct": 0.031}},
        "intelligence": {"active_event_count": 0, "bias_direction": "neutral", "summary": None},
        "policy": {"volatility_regime": "NORMAL"},
        "domain_signals": None,
    }


def _pack(**over):
    kw = dict(
        product_id="product_b", name_zh="材料B", day=7,
        history_rows=_history(start=7000.0, step=-0.8),
        upstream_histories={"cracked_c5": _history(start=5800.0, step=-0.5)},
        run_results={"7": {"base": [6660.0, 6650.0], "bull": [6900.0, 6950.0], "bear": [6400.0, 6350.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.09, "naive_mape": 0.11},
        explanation=_explanation(),
        as_of_month=8,
    )
    kw.update(over)
    return build_pack(**kw)


def test_pack_core_numbers():
    p = _pack()
    assert p["forecast_base"] == 6660.0
    assert p["forecast_end"] == 6650.0
    assert p["bull"] == 6950.0 and p["bear"] == 6350.0  # last point of curves
    assert p["expected_change_pct"] == -0.04
    assert p["p_rise"] == 0.42
    assert p["decision"]["action"] == "watch"
    assert p["trust"]["reason_codes"] == ["sparse_data"]
    assert len(p["drivers"]) == 3  # top-3 only


def test_pack_causal_transmission():
    p = _pack()
    up = p["upstream"][0]
    assert up["product_id"] == "cracked_c5"
    assert up["chg_30d_pct"] is not None and up["chg_30d_pct"] < 0
    assert p["causal"]["elasticity"] == 0.663
    assert p["causal"]["implied_pct"] < 0  # falling upstream → negative implied


def test_pack_divergence_flag():
    # upstream implies strong fall, model says rise → divergent
    p = _pack(explanation={**_explanation(),
                           "probability": {"7": {"p_rise": 0.6, "expected_change_pct": 0.05}}})
    assert p["causal"]["divergent"] is True
    p2 = _pack()  # both negative → not divergent
    assert p2["causal"]["divergent"] is False


def test_pack_seasonal_label():
    p = _pack()  # product_b month 8 → +1.5 in rules
    assert p["seasonal"]["adj_pct"] == 1.5
    assert p["seasonal"]["label_zh"] == "传统需求旺季"
    p_winter = _pack(as_of_month=12)
    assert p_winter["seasonal"]["label_zh"] == "传统需求淡季"


def test_pack_agreement_from_explanation():
    p = _pack()
    assert p["models"]["agreement"]["spread_pct"] == 0.031
    assert p["models"]["model_count"] == 8


def test_pack_handles_empty_explanation():
    p = _pack(explanation={}, run_results={}, model_detail=None)
    assert p["decision"]["action"] == "watch"
    assert p["p_rise"] is None
    assert p["drivers"] == []
    assert p["models"]["agreement"] is None


def test_price_percentile():
    rows = _history(start=100.0, n=100, step=1.0)  # rising; last = highest
    assert compute_price_percentile(rows) == 99.0
    assert compute_price_percentile(rows[:5]) is None  # <10 points


def test_upstream_map_covers_board_products():
    for pid in ("product_d", "product_f", "product_b", "feedstock_a", "sis",
                "product_a", "styrene", "blowing_agent", "cracked_c5", "cracked_c9"):
        assert pid in UPSTREAM_MAP
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_pack.py -v`
Expected: FAIL — `ImportError: cannot import name 'UPSTREAM_MAP'`

- [ ] **Step 3: Implement pack builder**

Add public alias in `market_dashboard_service.py` immediately after the `_read_product_history_rows` definition:
```python
def read_product_history_rows(product_id: str) -> list:
    """Public alias of _read_product_history_rows for the analyst pack."""
    return _read_product_history_rows(product_id)
```

Append to `evidence_pack.py`:
```python
from app.services.forecasting import narrative as fn
from app.services.forecasting.domain_signals import (
    compute_seasonal_adjustment, get_elasticity,
)

UPSTREAM_MAP: dict[str, list[str]] = {
    "product_d": ["cracked_c5"], "product_f": ["cracked_c5"],
    "product_b": ["cracked_c5"], "feedstock_a": ["cracked_c5"],
    "sis": ["cracked_c5"], "product_a": ["cracked_c5"],
    "styrene": ["cracked_c9"], "blowing_agent": ["cracked_c9"],
    "cracked_c5": ["product_c"], "cracked_c9": ["product_c"],
    "product_c": ["product_e"], "product_e": [],
}

_UPSTREAM_NAME_ZH = {
    "product_e": "原油", "product_c": "石脑油",
    "cracked_c5": "裂解原料A", "cracked_c9": "裂解材料M",
}


def compute_price_percentile(history_rows: list) -> Optional[float]:
    prices = [float(v) for _d, v in (history_rows or []) if v is not None]
    if len(prices) < 10:
        return None
    cur = prices[-1]
    below = sum(1 for p in prices if p < cur)
    return round(below / len(prices) * 100, 1)


def _horizon_stats(run_results: dict, day: int) -> dict:
    payload = (run_results or {}).get(str(day)) or (run_results or {}).get(f"{day}d") or {}
    unc = fn.compute_uncertainty_stats(payload)
    base = payload.get("base")
    first = float(base[0]) if isinstance(base, list) and base else (
        float(base) if isinstance(base, (int, float)) else None)
    return {"first": first, "unc": unc}


def build_pack(*, product_id, name_zh, day, history_rows, upstream_histories,
               run_results, model_detail, explanation, as_of_month) -> dict:
    exp = explanation or {}
    hs = _horizon_stats(run_results, day)
    prob = (exp.get("probability") or {}).get(str(day)) or {}
    dire = (exp.get("directional") or {}).get(str(day)) or {}
    dec = (exp.get("decision") or {}).get(str(day)) or {}
    trust_raw = exp.get("trust_tier") or {}
    intel_raw = exp.get("intelligence") or {}
    policy_raw = exp.get("policy") or {}
    agree = (exp.get("model_agreement") or {}).get(str(day))

    models = fn.compute_model_stats(model_detail, None)
    models["agreement"] = agree

    upstream = []
    for up_id in UPSTREAM_MAP.get(product_id, []):
        rows = (upstream_histories or {}).get(up_id) or []
        trend = fn.compute_trend_stats(rows)
        upstream.append({
            "product_id": up_id,
            "name_zh": _UPSTREAM_NAME_ZH.get(up_id, up_id),
            "chg_30d_pct": (round(trend["chg_30d_pct"] * 100, 1)
                            if trend.get("chg_30d_pct") is not None else None),
            "percentile": compute_price_percentile(rows),
        })

    elasticity = get_elasticity(product_id)
    implied = None
    divergent = False
    exp_pct = prob.get("expected_change_pct")
    if elasticity is not None and upstream and upstream[0]["chg_30d_pct"] is not None:
        implied = round(upstream[0]["chg_30d_pct"] * elasticity, 2)
        if exp_pct is not None:
            model_pct = exp_pct * 100.0
            divergent = (implied * model_pct < 0) and abs(implied - model_pct) > 2.0

    adj = compute_seasonal_adjustment(product_id, as_of_month)
    seasonal_label = ("传统需求淡季" if adj < 0 else "传统需求旺季") if adj != 0 else None

    drivers = [{"feature": d.get("feature"), "weight": d.get("weight")}
               for d in (exp.get("drivers") or [])[:3]
               if isinstance(d, dict) and d.get("feature")]

    return {
        "product_id": product_id, "name_zh": name_zh, "day": day,
        "current_price": fn.compute_trend_stats(history_rows).get("last_price"),
        "price_date": (history_rows[-1][0] if history_rows else None),
        "price_percentile": compute_price_percentile(history_rows),
        "forecast_base": hs["first"],
        "forecast_end": hs["unc"].get("base"),
        "bull": hs["unc"].get("bull"), "bear": hs["unc"].get("bear"),
        "spread_pct": hs["unc"].get("spread_pct"),
        "expected_change_pct": exp_pct, "p_rise": prob.get("p_rise"),
        "trend": fn.compute_trend_stats(history_rows),
        "models": models,
        "trust": {"tier": trust_raw.get("tier"),
                  "reason_zh": trust_raw.get("reason_zh"),
                  "reason_codes": list(trust_raw.get("reason_codes") or [])},
        "directional": {"accuracy": dire.get("accuracy"),
                        "status": dire.get("status"), "n_test": dire.get("n_test")},
        "decision": {"action": dec.get("action", "watch"),
                     "confidence": dec.get("confidence", "low"),
                     "rationale": dec.get("rationale")},
        "drivers": drivers,
        "upstream": upstream,
        "causal": {"elasticity": elasticity, "implied_pct": implied,
                   "divergent": divergent},
        "seasonal": {"month": as_of_month, "adj_pct": adj, "label_zh": seasonal_label},
        "intelligence": {"event_count": int(intel_raw.get("active_event_count", 0) or 0),
                         "bias": intel_raw.get("bias_direction", "neutral"),
                         "summary": intel_raw.get("summary")},
        "policy": {"volatility_regime": policy_raw.get("volatility_regime")},
    }
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_pack.py -v`
Expected: PASS

---

### Task 4: Template brief renderer (deterministic fallback)

**Files:**
- Create: `backend/app/services/forecasting/analyst/template_brief.py`
- Test: `backend/tests/test_forecast_analyst_template.py`

**Interfaces:**
- Consumes: Task 3 pack.
- Produces: `render_template_brief(pack: dict) -> dict` returning the brief schema used everywhere:
```python
{"summary_zh": str, "why_price_zh": str, "why_action_zh": str,
 "watch_triggers_zh": list[str], "risk_zh": str,
 "source": "template", "day": int, "generated_at": iso8601 str}
```

Content rules (zh):
- `summary_zh`: name + current price + percentile + forecast + expected change + p_rise + action label (reuse `decision_board_service._fmt_price/_fmt_pct/_fmt_prob` patterns — reimplement locally to avoid import cycle: analyst must NOT import decision_board_service because decision_board_service will import analyst).
- `why_price_zh`: ① upstream transmission (name, 30d change, elasticity, implied, divergence warning) ② trend (7d/30d, MA30 position) ③ seasonal label ④ top driver ⑤ model agreement (spread 较大 when > 0.02).
- `why_action_zh`: action advice + threshold logic mirroring `narrative.build_action_logic_zh` PLUS trust reason_zh/reason_codes in words (sparse_data→"历史数据不足"; below_naive_baseline→"模型回测弱于朴素基线"; weekly_cadence→"周频交易产品").
- `watch_triggers_zh`: 2-4 concrete re-evaluation triggers (directional accuracy ≥55%, probability thresholds, upstream stabilization when implied<0, seasonal window end).
- `risk_zh`: divergence flag / wide model spread / 0 intel events → "近期无重大市场事件,价格主要由供需基本面驱动" / high volatility. Empty string when none.

- [ ] **Step 1: Write the failing test**

```python
"""Template brief renderer — the deterministic fallback (PRODUCT_B case)."""
from app.services.forecasting.analyst.evidence_pack import build_pack
from app.services.forecasting.analyst.template_brief import render_template_brief
from tests.test_forecast_analyst_pack import _explanation, _history


def _product_b_pack():
    return build_pack(
        product_id="product_b", name_zh="材料B", day=7,
        history_rows=_history(start=7000.0, step=-0.8),
        upstream_histories={"cracked_c5": _history(start=5800.0, step=-0.5)},
        run_results={"7": {"base": [6660.0, 6650.0], "bull": [6900.0, 6950.0], "bear": [6400.0, 6350.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.09, "naive_mape": 0.11},
        explanation=_explanation(), as_of_month=8,
    )


def test_brief_schema_and_source():
    b = render_template_brief(_product_b_pack())
    for key in ("summary_zh", "why_price_zh", "why_action_zh",
                "watch_triggers_zh", "risk_zh", "source", "day", "generated_at"):
        assert key in b
    assert b["source"] == "template"
    assert isinstance(b["watch_triggers_zh"], list) and len(b["watch_triggers_zh"]) >= 2


def test_brief_explains_why_price():
    b = render_template_brief(_product_b_pack())
    assert "裂解原料A" in b["why_price_zh"]        # upstream named
    assert "弹性" in b["why_price_zh"]            # elasticity mentioned
    assert "传统需求旺季" in b["why_price_zh"]     # seasonal (product_b month 8 = +1.5)


def test_brief_explains_why_action_low_trust():
    b = render_template_brief(_product_b_pack())
    assert "观望" in b["why_action_zh"]
    assert "历史数据不足" in b["why_action_zh"]    # sparse_data reason in words
    assert "55%" in b["why_action_zh"]            # edge threshold referenced


def test_brief_watch_triggers_actionable():
    b = render_template_brief(_product_b_pack())
    joined = "。".join(b["watch_triggers_zh"])
    assert "55%" in joined                        # accuracy recovery trigger
    assert "裂解原料A" in joined or "上游" in joined  # upstream stabilization trigger


def test_brief_zero_intel_risk_note():
    b = render_template_brief(_product_b_pack())
    assert "近期无重大市场事件" in b["risk_zh"]


def test_brief_buy_case():
    exp = _explanation()
    exp["probability"] = {"7": {"p_rise": 0.78, "expected_change_pct": 0.045}}
    exp["directional"] = {"7": {"accuracy": 0.68, "status": "edge", "n_test": 60}}
    exp["decision"] = {"7": {"action": "buy", "confidence": "medium", "rationale": "r"}}
    exp["trust_tier"] = {"tier": "high", "reason_zh": "模型持续跑赢基准",
                          "reason_codes": ["model_skill_high"], "badge_label_zh": "高置信"}
    pack = build_pack(
        product_id="product_d", name_zh="电子元件", day=7,
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
        product_id="product_b", name_zh="材料B", day=7,
        history_rows=[], upstream_histories={},
        run_results={}, model_detail=None, explanation={}, as_of_month=8,
    )
    b = render_template_brief(pack)
    assert "数据不足" in b["summary_zh"]
    assert b["why_price_zh"] == "" or "暂无" in b["why_price_zh"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_template.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement template renderer**

Create `template_brief.py`:

```python
"""Deterministic brief renderer — enriched fallback for the LLM writer."""
from __future__ import annotations

from datetime import datetime, timezone

_ACTION_ADVICE_ZH = {
    "buy": "建议提前备货,锁定当前价格",
    "sell": "建议抓紧出货,兑现当前高价",
    "hold": "建议按需跟进,不追涨不囤货",
    "watch": "建议保持观望,等待更明确的信号",
}
_REASON_CODE_ZH = {
    "sparse_data": "历史数据不足",
    "below_naive_baseline": "模型回测弱于朴素基线",
    "weekly_cadence": "周频交易产品,日度精度有限",
    "model_skill_high": "模型回测持续跑赢基准",
    "model_skill_medium": "模型具备一定预测能力",
}
_BUY_P, _SELL_P, _MIN_CHG, _EDGE = 0.70, 0.30, 0.03, 0.55


def _fmt_price(v):
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct_signed(v, digits=1):
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_pct_plain(v, digits=0):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _summary(p: dict) -> str:
    if p.get("current_price") is None or p.get("forecast_base") is None:
        return f"{p['name_zh']}当前预测数据不足,建议观望。"
    move = "涨至" if (p.get("expected_change_pct") or 0) >= 0 else "跌至"
    s = (f"{p['name_zh']}现价 {_fmt_price(p['current_price'])} 元/吨")
    if p.get("price_percentile") is not None:
        s += f",处于近三年约 {p['price_percentile']:.0f}% 分位"
    s += (f"。预计未来 {p['day']} 天{move} {_fmt_price(p['forecast_base'])} 元/吨"
          f"({_fmt_pct_signed(p.get('expected_change_pct'))})")
    if p.get("p_rise") is not None:
        s += f",上涨概率 {_fmt_pct_plain(p['p_rise'])}"
    return s + "。"


def _why_price(p: dict) -> str:
    bits: list[str] = []
    up = (p.get("upstream") or [{}])[0]
    causal = p.get("causal") or {}
    if up.get("chg_30d_pct") is not None:
        seg = (f"上游{up['name_zh']}近30日{up['chg_30d_pct']:+.1f}%")
        if causal.get("elasticity") is not None and causal.get("implied_pct") is not None:
            seg += (f",按 {causal['elasticity']:.2f} 弹性传导约 "
                    f"{causal['implied_pct']:+.1f}%")
            if p.get("expected_change_pct") is not None:
                model_pct = p["expected_change_pct"] * 100
                seg += (",与模型预测方向一致" if not causal.get("divergent")
                        else f",而模型预测 {model_pct:+.1f}%,方向背离,需警惕")
        bits.append(seg)
    t = p.get("trend") or {}
    tb = []
    if t.get("chg_7d_pct") is not None:
        tb.append(f"近7日{_fmt_pct_signed(t['chg_7d_pct'])}")
    if t.get("chg_30d_pct") is not None:
        tb.append(f"近30日{_fmt_pct_signed(t['chg_30d_pct'])}")
    if t.get("above_ma30") is not None:
        tb.append("现价位于30日均线上方" if t["above_ma30"] else "现价位于30日均线下方")
    if tb:
        bits.append("趋势:" + ",".join(tb))
    if (p.get("seasonal") or {}).get("label_zh"):
        bits.append(f"当前为{p['seasonal']['month']}月{p['seasonal']['label_zh']}"
                    f"(季节调整 {p['seasonal']['adj_pct']:+.1f}%)")
    drivers = p.get("drivers") or []
    if drivers:
        bits.append(f"模型首要驱动因子:{drivers[0]['feature']}"
                    f"(权重 {drivers[0]['weight'] * 100:.0f}%)")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.02:
        bits.append(f"{ag['n_models']} 模型间分歧偏大(区间 "
                    f"{_fmt_price(ag['min'])}–{_fmt_price(ag['max'])})")
    if not bits:
        return "" if p.get("current_price") is None else "暂无可归因的价格驱动因素。"
    return "价格逻辑:" + ";".join(bits) + "。"


def _why_action(p: dict) -> str:
    action = (p.get("decision") or {}).get("action", "watch")
    advice = _ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH["watch"])
    reasons = [_REASON_CODE_ZH[c] for c in (p.get("trust") or {}).get("reason_codes", [])
               if c in _REASON_CODE_ZH]
    dire = p.get("directional") or {}
    if action == "watch":
        if dire.get("accuracy") is not None and dire.get("status") != "edge":
            reasons.append(f"方向准确率 {_fmt_pct_plain(dire['accuracy'])} 未达 "
                           f"{_fmt_pct_plain(_EDGE)} 显著门槛")
        why = "、".join(reasons) if reasons else "信号不明确"
        return (f"{advice} —— {why},不构成可操作信号。"
                f"(置信度:{(p.get('decision') or {}).get('confidence', 'low')})")
    if action == "buy":
        cond = f"上涨概率≥{_fmt_pct_plain(_BUY_P)} 且预期涨幅≥{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标"
    elif action == "sell":
        cond = f"上涨概率≤{_fmt_pct_plain(_SELL_P)} 且预期跌幅≥{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标"
    else:
        cond = "有方向优势但预期波动未达买卖阈值"
    tail = f"({';'.join(reasons)})" if reasons else ""
    return f"{advice} —— 满足条件:{cond}{tail}。"


def _triggers(p: dict) -> list[str]:
    out = [f"方向准确率回升至 {_fmt_pct_plain(_EDGE)} 以上且概率突破买卖阈值时,重新评估为可操作信号"]
    causal = p.get("causal") or {}
    up = (p.get("upstream") or [{}])[0]
    if causal.get("implied_pct") is not None and causal["implied_pct"] < 0 and up.get("name_zh"):
        out.append(f"上游{up['name_zh']}止跌企稳,将缓解成本端下压")
    if p.get("expected_change_pct") is not None and p["expected_change_pct"] < 0:
        out.append(f"若上涨概率突破 {_fmt_pct_plain(_BUY_P)} 且预期转正,升级为备货建议")
    else:
        out.append(f"若上涨概率跌破 {_fmt_pct_plain(_SELL_P)} 且预期跌幅超过 "
                   f"{_fmt_pct_plain(_MIN_CHG)},升级为出货建议")
    if (p.get("seasonal") or {}).get("label_zh") == "传统需求淡季":
        out.append("淡季窗口结束后关注需求恢复节奏")
    return out[:4]


def _risk(p: dict) -> str:
    risks: list[str] = []
    if (p.get("causal") or {}).get("divergent"):
        risks.append("上游传导与模型方向背离,预测不确定性升高")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.05:
        risks.append("模型间分歧显著,点预测参考价值有限")
    if (p.get("policy") or {}).get("volatility_regime") == "HIGH":
        risks.append("当前波动剧烈,注意仓位风险")
    if (p.get("intelligence") or {}).get("event_count", 0) == 0:
        risks.append("近期无重大市场事件,价格主要由供需基本面驱动")
    return "风险提示:" + ";".join(risks) + "。" if risks else ""


def render_template_brief(pack: dict) -> dict:
    return {
        "summary_zh": _summary(pack),
        "why_price_zh": _why_price(pack),
        "why_action_zh": _why_action(pack),
        "watch_triggers_zh": _triggers(pack),
        "risk_zh": _risk(pack),
        "source": "template",
        "day": pack.get("day"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_template.py -v`
Expected: PASS

---

### Task 5: LLM brief writer + output verifier

**Files:**
- Create: `backend/app/services/forecasting/analyst/brief_writer.py`
- Create: `backend/app/services/forecasting/analyst/verifier.py`
- Test: `backend/tests/test_forecast_analyst_writer.py`
- Test: `backend/tests/test_forecast_analyst_verifier.py`

**Interfaces:**
- Produces: `BRIEF_JSON_SCHEMA: dict`, `build_analyst_prompt(pack: dict) -> str`
- Produces: `write_brief_llm(pack: dict, llm_caller) -> dict | None` — `llm_caller(prompt: str, schema: dict) -> dict` (signature matches `chat_completion_json_sync(prompt, schema)`; injectable for tests). Returns brief dict with `source: "llm"` or None on any failure.
- Produces: `verify_brief(brief: dict, pack: dict) -> list[str]` — violations; empty = pass.
- Consumes: Task 3 pack, Task 4 brief schema.

Verifier rules:
1. Required keys: `summary_zh, why_price_zh, why_action_zh, watch_triggers_zh, risk_zh`; `watch_triggers_zh` must be a list.
2. Every number extracted from brief text (regex `[-+]?\d[\d,]*\.?\d*`, commas stripped) must match some pack number within tolerance `max(0.51, |n| * 0.005)`. Pack number set = all floats/ints found recursively in the pack, plus derived variants: `x*100` rounded to 0 and 1 decimals (fraction↔percent), and each rounded to 0/1/2 decimals.
3. Numbers < 10 (e.g. day counts, elasticity like 0.66, month 8, weights) are exempt only if ≤ 10 — instead: exempt numbers ≤ 12 that appear verbatim in the pack OR equal pack["day"] OR pack["seasonal"]["month"]. Simpler robust rule: numbers ≤ 12 are exempt from matching (small ints are generic). Numbers > 12 must match.

- [ ] **Step 1: Write the failing tests**

`test_forecast_analyst_verifier.py`:
```python
"""Brief output verifier — the anti-hallucination gate."""
from app.services.forecasting.analyst.verifier import verify_brief
from tests.test_forecast_analyst_template import _product_b_pack


def _brief(**over):
    b = {
        "summary_zh": "材料B现价 6,940 元/吨,预计未来 7 天跌至 6,660 元/吨(-4.0%)。",
        "why_price_zh": "上游裂解原料A近30日-2.8%,按 0.66 弹性传导约 -1.9%。",
        "why_action_zh": "建议观望 —— 历史数据不足,方向准确率 51% 未达 55% 门槛。",
        "watch_triggers_zh": ["方向准确率回升至 55% 以上"],
        "risk_zh": "近期无重大市场事件。",
    }
    b.update(over)
    return b


def test_valid_brief_passes():
    pack = _product_b_pack()
    # numbers: 6940 (current), 6660 (base), -4.0 (exp*100), 51 (acc*100), 55 threshold
    assert verify_brief(_brief(), pack) == []


def test_hallucinated_price_rejected():
    pack = _product_b_pack()
    v = verify_brief(_brief(summary_zh="现价 7,200 元/吨,预计涨至 7,500 元/吨(+4.2%)。"), pack)
    assert any("7200" in msg or "7500" in msg for msg in v)
    assert v != []


def test_missing_key_rejected():
    b = _brief()
    del b["why_price_zh"]
    assert verify_brief(b, _product_b_pack()) != []


def test_triggers_must_be_list():
    assert verify_brief(_brief(watch_triggers_zh="not a list"), _product_b_pack()) != []


def test_small_numbers_exempt():
    # 7 (day), 8 (month), 3 (count) — all ≤12, exempt
    assert verify_brief(_brief(why_price_zh="8 模型集成,7 日维度,3 大因素。"), _product_b_pack()) == []
```

`test_forecast_analyst_writer.py`:
```python
"""LLM brief writer — prompt contract + mocked LLM."""
from app.services.forecasting.analyst.brief_writer import (
    BRIEF_JSON_SCHEMA, build_analyst_prompt, write_brief_llm,
)
from tests.test_forecast_analyst_template import _product_b_pack


def test_prompt_contains_evidence_and_contract():
    prompt = build_analyst_prompt(_product_b_pack())
    assert "只能使用" in prompt or "ONLY" in prompt.upper()
    assert "6660" in prompt or "6,660" in prompt   # forecast base in evidence
    assert "材料B" in prompt
    assert "JSON" in prompt


def test_writer_success_marks_llm_source():
    pack = _product_b_pack()
    good = {
        "summary_zh": "现价 6,940 元/吨,预计 7 日跌至 6,660 元/吨(-4.0%),建议观望。",
        "why_price_zh": "上游裂解原料A近30日下跌,按 0.66 弹性传导约 -1.9%。",
        "why_action_zh": "历史数据不足,方向准确率 51% 未达 55% 门槛,观望。",
        "watch_triggers_zh": ["准确率回升至 55% 以上再评估"],
        "risk_zh": "近期无重大市场事件,价格由供需基本面驱动。",
    }
    out = write_brief_llm(pack, llm_caller=lambda prompt, schema: dict(good))
    assert out is not None
    assert out["source"] == "llm"
    assert out["summary_zh"] == good["summary_zh"]


def test_writer_returns_none_on_empty_llm():
    assert write_brief_llm(_product_b_pack(), llm_caller=lambda p, s: {}) is None


def test_writer_returns_none_on_hallucination():
    pack = _product_b_pack()
    bad = {"summary_zh": "现价 9,999 元/吨,暴涨 50%。", "why_price_zh": "x",
           "why_action_zh": "y", "watch_triggers_zh": [], "risk_zh": "z"}
    assert write_brief_llm(pack, llm_caller=lambda p, s: dict(bad)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_verifier.py tests/test_forecast_analyst_writer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`verifier.py`:
```python
"""Output verifier — every number in a brief must exist in the evidence pack."""
from __future__ import annotations

import re

REQUIRED_KEYS = ("summary_zh", "why_price_zh", "why_action_zh",
                 "watch_triggers_zh", "risk_zh")
_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_SMALL_NUM_EXEMPT = 12.0


def _pack_numbers(pack: dict) -> set[float]:
    nums: set[float] = set()

    def _add(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        for cand in (v, v * 100.0):
            for d in (0, 1, 2):
                nums.add(round(float(cand), d))

    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                _walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                _walk(v)
        else:
            _add(x)

    _walk(pack)
    return nums


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.51, abs(b) * 0.005)


def verify_brief(brief: dict, pack: dict) -> list[str]:
    violations: list[str] = []
    if not isinstance(brief, dict):
        return ["brief is not a dict"]
    for key in REQUIRED_KEYS:
        if key not in brief:
            violations.append(f"missing required key: {key}")
    if violations:
        return violations
    if not isinstance(brief.get("watch_triggers_zh"), list):
        return ["watch_triggers_zh must be a list"]

    known = _pack_numbers(pack)
    texts = [str(brief.get(k) or "") for k in
             ("summary_zh", "why_price_zh", "why_action_zh", "risk_zh")]
    texts += [str(t) for t in brief.get("watch_triggers_zh") or []]
    for m in _NUM_RE.finditer(" ".join(texts)):
        raw = m.group(0).replace(",", "").lstrip("+")
        try:
            n = float(raw)
        except ValueError:
            continue
        if abs(n) <= _SMALL_NUM_EXEMPT:
            continue
        if not any(_close(n, k) for k in known):
            violations.append(f"number not in evidence pack: {m.group(0)}")
    return violations
```

`brief_writer.py`:
```python
"""LLM analyst brief writer — grounded prose from the evidence pack only."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.services.forecasting.analyst.verifier import verify_brief

logger = logging.getLogger(__name__)

BRIEF_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_zh": {"type": "string"},
        "why_price_zh": {"type": "string"},
        "why_action_zh": {"type": "string"},
        "watch_triggers_zh": {"type": "array", "items": {"type": "string"}},
        "risk_zh": {"type": "string"},
    },
    "required": ["summary_zh", "why_price_zh", "why_action_zh",
                 "watch_triggers_zh", "risk_zh"],
}


def build_analyst_prompt(pack: dict) -> str:
    return (
        "你是石化市场分析师。根据下方 EVIDENCE_JSON 撰写该产品的 AI 简评。\n"
        "严格规则:\n"
        "1. 只能使用 EVIDENCE_JSON 中已提供的数字和事实,禁止自行添加任何数字。\n"
        "2. 不要改变决策:decision.action 已由确定性引擎给出,你只能解释它。\n"
        "3. 输出简体中文 JSON,字段:summary_zh(一句话总览), why_price_zh(价格逻辑:"
        "上游传导/趋势/季节/模型驱动), why_action_zh(建议逻辑:为什么是这个操作建议,"
        "信任等级原因), watch_triggers_zh(2-4 条重新评估触发条件,字符串数组), "
        "risk_zh(风险提示)。\n"
        "4. 即使置信度低、建议观望,也必须解释清楚为什么以及应该关注什么。\n\n"
        f"EVIDENCE_JSON:\n{json.dumps(pack, ensure_ascii=False, default=str)}"
    )


def write_brief_llm(pack: dict, llm_caller) -> dict | None:
    """Call the LLM and return a verified brief, or None on any failure.

    llm_caller: callable(prompt: str, schema: dict) -> dict
    (matches llm_service.chat_completion_json_sync's call shape).
    """
    try:
        out = llm_caller(build_analyst_prompt(pack), BRIEF_JSON_SCHEMA)
    except Exception as exc:
        logger.warning("[analyst] LLM brief call failed: %s", exc)
        return None
    if not isinstance(out, dict) or not out:
        return None
    violations = verify_brief(out, pack)
    if violations:
        logger.warning("[analyst] brief rejected by verifier: %s", violations)
        return None
    out["source"] = "llm"
    out["day"] = pack.get("day")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_verifier.py tests/test_forecast_analyst_writer.py -v`
Expected: PASS

---

### Task 6: Analyst service — cache + orchestration + config flag

**Files:**
- Create: `backend/app/services/forecasting/analyst/service.py`
- Modify: `backend/app/config.py:390` (add two flags)
- Test: `backend/tests/test_forecast_analyst_service.py`

**Interfaces:**
- Consumes: Tasks 3/4/5, `mds.read_product_history_rows` (Task 3), `mds.PRODUCT_FORECAST_TARGET_KEY`, `PRODUCT_LABELS`.
- Produces: `get_analyst_brief(product_id: str, day: int = 7, db=None, persist: bool = True) -> dict | None`
  - Cache: `(run.explanation or {}).get("analyst_brief", {}).get(str(day))` → return as-is.
  - Miss: build pack (history + upstream histories via `mds.read_product_history_rows`) → `render_template_brief` → if `FORECAST_ANALYST_ANALYST_LLM_ENABLED`: `write_brief_llm(pack, chat_completion_json_sync)`; keep template on None.
  - Persist: mutate `run.explanation["analyst_brief"][str(day)] = brief`; `db.commit()` only when `persist=True`; commit failures swallowed (log + rollback).
  - Returns None when product has no target/run.
- Produces: `prewarm_brief(product_id, day, db)` = `get_analyst_brief(..., persist=False)` (engine calls it; caller commits).

- [ ] **Step 1: Write the failing test**

```python
"""Analyst service — cache, fallback chain, persistence."""
from types import SimpleNamespace

from app.services.forecasting.analyst import service as analyst_service
from tests.test_forecast_analyst_pack import _explanation, _history


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a): return self
    def order_by(self, *a): return self
    def first(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, target, run): self._target, self._run = target, run; self.commits = 0
    def query(self, model):
        name = getattr(model, "__name__", "")
        return _FakeQuery([self._target] if "Target" in name else [self._run])
    def commit(self): self.commits += 1
    def rollback(self): pass


def _run(explanation):
    return SimpleNamespace(results={"7": {"base": [6660.0], "bull": [6900.0], "bear": [6400.0]}},
                           model_detail={"models_run": ["a"] * 8},
                           explanation=explanation)


def _patch_reads(monkeypatch):
    monkeypatch.setattr(analyst_service.mds, "read_product_history_rows",
                        lambda pid: _history(start=7000.0, step=-0.8))
    monkeypatch.setattr(analyst_service.mds, "PRODUCT_FORECAST_TARGET_KEY",
                        {"product_b": "acme.product_b"})
    monkeypatch.setattr(analyst_service, "PRODUCT_LABELS",
                        {"product_b": {"label_zh": "材料B"}})


def test_cache_hit_returns_without_llm(monkeypatch):
    _patch_reads(monkeypatch)
    cached = {"summary_zh": "cached", "source": "llm", "day": 7}
    run = _run({**_explanation(), "analyst_brief": {"7": cached}})
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: (_ for _ in ()).throw(AssertionError("LLM called on cache hit")))
    assert analyst_service.get_analyst_brief("product_b", 7, db=db) == cached
    assert db.commits == 0


def test_miss_builds_template_and_persists(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: False)
    brief = analyst_service.get_analyst_brief("product_b", 7, db=db)
    assert brief is not None and brief["source"] == "template"
    assert run.explanation["analyst_brief"]["7"]["source"] == "template"
    assert db.commits == 1


def test_llm_upgrade_when_enabled(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: True)
    good = {"summary_zh": "现价 6,940 元/吨,预计跌至 6,660 元/吨。",
            "why_price_zh": "上游裂解原料A下跌,弹性 0.66 传导。",
            "why_action_zh": "方向准确率 51% 未达 55% 门槛,观望。",
            "watch_triggers_zh": ["准确率 55% 以上再评估"], "risk_zh": "近期无重大市场事件。"}
    monkeypatch.setattr(analyst_service, "chat_completion_json_sync",
                        lambda prompt, schema: dict(good))
    brief = analyst_service.get_analyst_brief("product_b", 7, db=db)
    assert brief["source"] == "llm"


def test_llm_failure_falls_back_to_template(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: True)
    monkeypatch.setattr(analyst_service, "chat_completion_json_sync", lambda p, s: {})
    assert analyst_service.get_analyst_brief("product_b", 7, db=db)["source"] == "template"


def test_unknown_product_returns_none(monkeypatch):
    monkeypatch.setattr(analyst_service.mds, "PRODUCT_FORECAST_TARGET_KEY", {})
    assert analyst_service.get_analyst_brief("nope", 7, db=_FakeDB(None, None)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_service.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Add to `app/config.py` after `FORECAST_DRIFT_THRESHOLD_PCT` (:390):
```python
    FORECAST_DOMAIN_SIGNALS_ENABLED: bool = False
    FORECAST_ANALYST_LLM_ENABLED: bool = False
```

Create `service.py`:
```python
"""Analyst brief service — cached LLM/template briefs on ForecastRun rows."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.models.market_dashboard import PRODUCT_LABELS
from app.services import market_dashboard_service as mds
from app.services.llm_service import chat_completion_json_sync
from app.services.forecasting.analyst.brief_writer import write_brief_llm
from app.services.forecasting.analyst.evidence_pack import UPSTREAM_MAP, build_pack
from app.services.forecasting.analyst.template_brief import render_template_brief

logger = logging.getLogger(__name__)


def _llm_enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "FORECAST_ANALYST_LLM_ENABLED", False))
    except Exception:
        return False


def _latest_run(product_id: str, db):
    from app.models.forecasting import ForecastRun, ForecastTarget
    target_key = mds.PRODUCT_FORECAST_TARGET_KEY.get(product_id)
    if not target_key:
        return None
    tgt = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == target_key,
        ForecastTarget.org_id == "default-org",
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).first()
    if tgt is None:
        return None
    return db.query(ForecastRun).filter(
        ForecastRun.target_id == tgt.id,
        ForecastRun.is_deleted == False,  # noqa: E712
    ).order_by(ForecastRun.created_date.desc()).first()


def _build_pack_for(product_id: str, day: int, run) -> dict:
    labels = PRODUCT_LABELS.get(product_id, {})
    upstream_histories = {
        up: mds.read_product_history_rows(up)
        for up in UPSTREAM_MAP.get(product_id, [])
    }
    return build_pack(
        product_id=product_id,
        name_zh=labels.get("label_zh", product_id),
        day=day,
        history_rows=mds.read_product_history_rows(product_id),
        upstream_histories=upstream_histories,
        run_results=run.results or {},
        model_detail=run.model_detail,
        explanation=run.explanation or {},
        as_of_month=datetime.now().month,
    )


def get_analyst_brief(product_id: str, day: int = 7, db=None,
                      persist: bool = True) -> Optional[dict]:
    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        run = _latest_run(product_id, db)
        if run is None or not run.results:
            return None
        cached = ((run.explanation or {}).get("analyst_brief") or {}).get(str(day))
        if cached:
            return cached

        pack = _build_pack_for(product_id, day, run)
        brief = render_template_brief(pack)
        if _llm_enabled():
            llm_brief = write_brief_llm(pack, chat_completion_json_sync)
            if llm_brief is not None:
                brief = llm_brief

        explanation = dict(run.explanation or {})
        briefs = dict(explanation.get("analyst_brief") or {})
        briefs[str(day)] = brief
        explanation["analyst_brief"] = briefs
        run.explanation = explanation
        if persist:
            try:
                db.commit()
            except Exception as exc:
                logger.warning("[analyst] brief persist failed: %s", exc)
                db.rollback()
        return brief
    except Exception as exc:
        logger.warning("[analyst] get_analyst_brief failed for %s: %s", product_id, exc)
        return None
    finally:
        if owns_session:
            db.close()


def prewarm_brief(product_id: str, day: int, db) -> None:
    """Engine-time pre-warm: build + attach brief in-session (caller commits)."""
    get_analyst_brief(product_id, day=day, db=db, persist=False)
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_service.py -v`
Expected: PASS

---

### Task 7: Decision Board integration

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py:167` (add `AnalystBriefMeta` + field)
- Modify: `backend/app/services/decision_board_service.py:186-268`
- Test: `backend/tests/test_service_decision_board.py` (extend)

**Interfaces:**
- Consumes: Task 6 `analyst_service.get_analyst_brief`.
- Produces: `AnalystBriefMeta` pydantic model: `summary_zh: str = ""`, `why_price_zh: str = ""`, `why_action_zh: str = ""`, `watch_triggers_zh: list[str] = []`, `risk_zh: str = ""`, `source: str = "template"`, `day: Optional[int] = None`, `generated_at: Optional[str] = None`.
- Produces: `DecisionBoardProduct.analyst_brief: Optional[AnalystBriefMeta] = None`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_service_decision_board.py`)

```python
def test_board_attaches_analyst_brief(monkeypatch):
    brief = {
        "summary_zh": "材料B现价 6,940 元/吨,预计跌至 6,660 元/吨。",
        "why_price_zh": "上游裂解原料A下跌。",
        "why_action_zh": "历史数据不足,观望。",
        "watch_triggers_zh": ["准确率 55% 以上再评估"],
        "risk_zh": "近期无重大市场事件。",
        "source": "template", "day": 7, "generated_at": "2026-08-04T00:00:00+00:00",
    }
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot(pid, 6940.0)
                       for pid in ("product_d", "product_f", "product_b", "feedstock_a")],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day,
            base_by_product={pid: 6660.0 for pid in product_ids},
            overlay_by_product={pid: _overlay(pid, action="watch", conf="low") for pid in product_ids},
            sku_forecasts_by_product={},
        ),
    )
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: brief)
    resp = dbs.build_decision_board("midstream", day=7)
    p = next(p for p in resp.products if p.id == "product_b")
    assert p.analyst_brief is not None
    assert p.analyst_brief.why_price_zh == "上游裂解原料A下跌。"
    assert p.analyst_brief.watch_triggers_zh == ["准确率 55% 以上再评估"]
    assert p.narrative_zh.startswith("材料B现价 6,940")


def test_board_survives_analyst_failure(monkeypatch):
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot("product_d", 12100.0)],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day, base_by_product={"product_d": 12450.0},
            overlay_by_product={"product_d": _overlay("product_d")},
            sku_forecasts_by_product={},
        ),
    )
    monkeypatch.setattr(dbs, "PRODUCT_GROUP_MAP", {"midstream": ["product_d"]})
    from app.services.forecasting.analyst import service as analyst_service
    def _boom(pid, day=7, db=None):
        raise RuntimeError("analyst down")
    monkeypatch.setattr(analyst_service, "get_analyst_brief", _boom)
    resp = dbs.build_decision_board("midstream", day=7)
    assert resp.products[0].analyst_brief is None
    assert "12,100" in resp.products[0].narrative_zh  # legacy narrative intact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_service_decision_board.py -v`
Expected: FAIL — `AttributeError: 'DecisionBoardProduct' object has no attribute 'analyst_brief'` (pydantic ValidationError)

- [ ] **Step 3: Implement**

In `schemas/market_dashboard.py`, before `class DecisionBoardProduct`:
```python
class AnalystBriefMeta(BaseModel):
    """Structured AI 简评 — evidence-grounded analyst brief."""
    summary_zh: str = ""
    why_price_zh: str = ""
    why_action_zh: str = ""
    watch_triggers_zh: list[str] = []
    risk_zh: str = ""
    source: str = "template"           # "llm" | "template"
    day: Optional[int] = None
    generated_at: Optional[str] = None
```
Add to `DecisionBoardProduct` (after `narrative_zh`):
```python
    analyst_brief: Optional[AnalystBriefMeta] = None
```

In `decision_board_service.py` per-product loop, after `basis_zh/action_logic_zh` extraction (:225-226), add:
```python
        # Analyst brief (LLM/template, evidence-grounded) — additive
        brief = None
        try:
            from app.services.forecasting.analyst import service as analyst_service
            brief = analyst_service.get_analyst_brief(pid, day=day, db=db)
        except Exception:
            brief = None
        if brief:
            narrative_zh = (
                brief.get("summary_zh", "")
                + brief.get("why_price_zh", "")
                + brief.get("why_action_zh", "")
            ) or build_narrative(...)
            brief_meta = AnalystBriefMeta(
                **{k: brief.get(k) for k in (
                    "summary_zh", "why_price_zh", "why_action_zh",
                    "watch_triggers_zh", "risk_zh", "source", "day", "generated_at")}
            )
        else:
            narrative_zh = build_narrative(...)  # existing call unchanged
            brief_meta = None
```
Import `AnalystBriefMeta` from schemas at top; pass `narrative_zh=narrative_zh, analyst_brief=brief_meta` into `DecisionBoardProduct(...)` replacing the inline `build_narrative(...)` call.

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_service_decision_board.py tests/test_router_decision_board.py -v`
Expected: PASS

---

### Task 8: `forecast_brief` chat tool

**Files:**
- Modify: `backend/app/services/tool_handlers/forecast_tool.py` (append handler + schema + registration tuple)
- Test: `backend/tests/test_forecast_brief_tool.py`

**Interfaces:**
- Consumes: Task 6 service; tool pattern from existing `_forecast_list_skus`.
- Produces: tool `forecast_brief(product_id: str, day: int = 7)` → `{success, product_id, day, brief}` or `{success: False, error}`.

- [ ] **Step 1: Write the failing test**

```python
"""forecast_brief chat tool."""
import asyncio

from app.services.tool_handlers import forecast_tool
from app.services.forecasting.analyst import service as analyst_service


def test_forecast_brief_success(monkeypatch):
    brief = {"summary_zh": "材料B现价 6,940 元/吨。",
             "why_price_zh": "上游下跌。", "why_action_zh": "观望。",
             "watch_triggers_zh": ["t1"], "risk_zh": "", "source": "template", "day": 7}
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: brief)
    out = asyncio.run(forecast_tool._forecast_brief(
        {"product_id": "product_b", "day": 7}, db=None, user_id=None, context=None))
    assert out["success"] is True
    assert out["brief"]["summary_zh"].startswith("材料B")
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
    assert registry.get("forecast_brief") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_brief_tool.py -v`
Expected: FAIL — `AttributeError: module has no attribute '_forecast_brief'`

- [ ] **Step 3: Implement**

Append to `forecast_tool.py` (before the registration block):
```python
async def _forecast_brief(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Return the cached evidence-grounded analyst brief for a product.

    Required args:
        product_id  (str): Dashboard product_id (e.g. 'product_b', 'product_d').
    Optional args:
        day         (int): Forecast horizon in days (default 7).
    """
    product_id = args.get("product_id")
    if not product_id:
        return {"success": False, "error": "product_id is required."}
    day = int(args.get("day", 7))

    from app.services.forecasting.analyst import service as analyst_service

    def _do_work():
        return analyst_service.get_analyst_brief(product_id, day=day, db=db)

    try:
        brief = await asyncio.to_thread(_do_work)
    except Exception as exc:
        logger.warning("forecast_brief failed (%s): %s", product_id, exc)
        return {"success": False, "error": f"Brief generation failed: {exc}"}
    if brief is None:
        return {"success": False,
                "error": f"No forecast data or brief available for '{product_id}'."}
    return {"success": True, "product_id": product_id, "day": day, "brief": brief}


FORECAST_BRIEF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_brief",
        "description": (
            "Get the evidence-grounded AI analyst brief for a dashboard product "
            "(product_b, product_d, product_f, product_a, etc.): why the price is "
            "forecast to move (upstream transmission, trend, seasonality, model "
            "drivers), why the current buy/hold/sell/watch recommendation, and "
            "what triggers would change it. Use this to answer 'why' questions "
            "about the forecast. Safe read-only tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string",
                               "description": "Dashboard product_id, e.g. 'product_b'."},
                "day": {"type": "integer", "default": 7,
                        "description": "Forecast horizon in days (default 7)."},
            },
            "required": ["product_id"],
        },
    },
}
```
Add to the registration tuple list:
```python
    (
        "forecast_brief",
        FORECAST_BRIEF_SCHEMA,
        _forecast_brief,
        True,
        "Get the evidence-grounded AI analyst brief for a product.",
    ),
```

Note: `test_forecast_brief_registered` requires the registry `.get()` API — check `tool_registry.py` for the exact accessor name (e.g. `registry.get(name)`); adjust the test to match if different.

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_brief_tool.py -v`
Expected: PASS

---

### Task 9: Weekly report integration

**Files:**
- Modify: `backend/app/services/weekly_report/generator.py:164-183`
- Test: `backend/tests/test_weekly_report_analyst.py`

**Interfaces:**
- Consumes: Task 6 service. Keeps existing placeholder as fallback.

- [ ] **Step 1: Write the failing test**

```python
"""Weekly report — analyst brief replaces the placeholder narrative."""
from app.services.weekly_report import generator as wrg
from app.services.forecasting.analyst import service as analyst_service


def test_section_uses_analyst_brief(monkeypatch):
    monkeypatch.setattr(wrg, "collect_all_numbers",
                        lambda pid, db: {"current_price": 6940.0, "change_7d_pct": -1.2})
    monkeypatch.setattr(wrg, "render_chart_stub", lambda *a, **k: None)
    brief = {"summary_zh": "材料B现价 6,940 元/吨,预计下周走弱。",
             "why_price_zh": "上游裂解原料A下跌,成本端下移。",
             "why_action_zh": "建议观望。", "watch_triggers_zh": [], "risk_zh": "",
             "source": "template", "day": 7}
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: brief)
    svc = wrg.WeeklyReportService(db=None)
    section = svc._build_product_section("product_b", "2026-08-03", "2026-08-09", {}, False, False)
    assert "6,940" in section["narrative"]
    assert "成本端下移" in section["narrative"]


def test_section_falls_back_to_placeholder(monkeypatch):
    monkeypatch.setattr(wrg, "collect_all_numbers",
                        lambda pid, db: {"current_price": None})
    monkeypatch.setattr(wrg, "render_chart_stub", lambda *a, **k: None)
    monkeypatch.setattr(analyst_service, "get_analyst_brief",
                        lambda pid, day=7, db=None: None)
    svc = wrg.WeeklyReportService(db=None)
    section = svc._build_product_section("product_b", "2026-08-03", "2026-08-09", {}, False, False)
    assert "市场运行平稳" in section["narrative"]  # legacy placeholder
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_weekly_report_analyst.py -v`
Expected: FAIL — assertion on narrative content

- [ ] **Step 3: Implement**

In `generator.py:_build_product_section`, replace the narrative placeholder block (:168-173):
```python
        # Build narrative: analyst brief (evidence-grounded) → placeholder fallback
        narrative = None
        try:
            from app.services.forecasting.analyst import service as analyst_service
            brief = analyst_service.get_analyst_brief(pid, day=7, db=self.db)
            if brief:
                narrative = (
                    brief.get("summary_zh", "")
                    + brief.get("why_price_zh", "")
                    + brief.get("why_action_zh", "")
                ) or None
        except Exception as exc:
            logger.warning("[weekly-report] analyst brief failed for %s: %s", pid, exc)
        if not narrative:
            narrative = (
                f"本周{name_cn}市场运行平稳。"
                f"上游原料端变化幅度有限，需求端按需采购。"
                f"下周市场预计延续当前态势，需关注宏观面变化。"
            )
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_weekly_report_analyst.py -v`
Expected: PASS

---

### Task 10: Engine pre-warm hook

**Files:**
- Modify: `backend/app/services/forecasting/engine.py:736` (after `_apply_rules`, before `return run`)
- Test: `backend/tests/test_forecast_analyst_prewarm.py`

**Interfaces:**
- Consumes: Task 6 `prewarm_brief`. Produces: engine module-level `_prewarm_analyst_brief(db, product_key)` helper (never raises).

- [ ] **Step 1: Write the failing test**

```python
"""Engine analyst pre-warm hook."""
from app.services.forecasting import engine as engine_mod


def test_prewarm_calls_service_for_acme_product(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool",
                        lambda key, default=True: key == "FORECAST_ANALYST_LLM_ENABLED")
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="acme.product_b")
    assert calls == [("product_b", 7)]


def test_prewarm_skips_when_flag_off(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: False)
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="acme.product_b")
    assert calls == []


def test_prewarm_skips_non_acme_keys(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: True)
    from app.services.forecasting.analyst import service as analyst_service
    monkeypatch.setattr(analyst_service, "prewarm_brief",
                        lambda pid, day, db: calls.append((pid, day)))
    engine_mod._prewarm_analyst_brief(db=None, product_key="other.something")
    engine_mod._prewarm_analyst_brief(db=None, product_key=None)
    assert calls == []


def test_prewarm_swallows_service_errors(monkeypatch):
    monkeypatch.setattr(engine_mod, "_get_config_bool", lambda key, default=True: True)
    from app.services.forecasting.analyst import service as analyst_service
    def _boom(pid, day, db):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(analyst_service, "prewarm_brief", _boom)
    engine_mod._prewarm_analyst_brief(db=None, product_key="acme.product_b")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_prewarm.py -v`
Expected: FAIL — `AttributeError: module has no attribute '_prewarm_analyst_brief'`

- [ ] **Step 3: Implement**

Append to `engine.py` (module level, near `_get_config_bool`):
```python
def _prewarm_analyst_brief(db, product_key) -> None:
    """Best-effort analyst brief pre-warm after a forecast run. Never raises."""
    try:
        if not _get_config_bool("FORECAST_ANALYST_LLM_ENABLED", False):
            return
        parts = (product_key or "").split(".")
        if len(parts) != 2 or parts[0] != "acme":
            return
        from app.services.forecasting.analyst import service as analyst_service
        analyst_service.prewarm_brief(parts[1], day=7, db=db)
    except Exception as exc:
        logger.warning("[analyst] prewarm failed for %s: %s", product_key, exc)
```
Call it in `compute_target` after `_apply_rules(self._db, target_id, run)` (:736), before `return run`:
```python
        _prewarm_analyst_brief(self._db, target.product_key)
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_forecast_analyst_prewarm.py -v`
Expected: PASS

---

### Task 11: Frontend — structured brief in the expanded card

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx:216-225` (ExpandedCard right column)
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx` (extend)

**Interfaces:**
- Consumes: Task 7 `p.analyst_brief` (`{summary_zh, why_price_zh, why_action_zh, watch_triggers_zh[], risk_zh, source}`).

Rendering (inside `ExpandedCard`, replacing the single `{p.narrative_zh}` paragraph when `p.analyst_brief` exists):
```jsx
{p.analyst_brief ? (
  <div className="space-y-2 text-sm leading-relaxed text-slate-700 dark:text-slate-200">
    <p>{p.analyst_brief.summary_zh}</p>
    {p.analyst_brief.why_price_zh && <p>{p.analyst_brief.why_price_zh}</p>}
    {p.analyst_brief.why_action_zh && <p>{p.analyst_brief.why_action_zh}</p>}
    {p.analyst_brief.watch_triggers_zh?.length > 0 && (
      <div>
        <div className="text-xs font-medium text-slate-500 dark:text-slate-400">关注触发</div>
        <ul className="mt-0.5 list-disc pl-4 text-xs text-slate-500 dark:text-slate-400">
          {p.analyst_brief.watch_triggers_zh.map((t, i) => <li key={i}>{t}</li>)}
        </ul>
      </div>
    )}
    {p.analyst_brief.risk_zh && (
      <p className="text-xs text-amber-600 dark:text-amber-400">{p.analyst_brief.risk_zh}</p>
    )}
    {p.analyst_brief.source === 'llm' && (
      <span className="inline-block rounded bg-violet-50 px-1.5 py-0.5 text-[10px] text-violet-600 dark:bg-violet-900/30 dark:text-violet-300">AI 分析师撰写</span>
    )}
  </div>
) : (
  <p className="text-sm leading-relaxed text-slate-700 dark:text-slate-200">{p.narrative_zh}</p>
)}
```
Also in the stats grid, when `p.intel_event_count === 0` render `0 条` with a sub-line `近期无重大市场事件` (text-[10px] text-slate-400).

- [ ] **Step 1: Write the failing test** (append to `DecisionBoard.test.jsx`, following its existing render patterns — read it first to match its setup/mocks):

```jsx
it('renders structured analyst brief sections when present', () => {
  // reuse the file's existing mocked product, add analyst_brief:
  const product = {
    /* ...existing minimal product fields per the file's fixtures... */
    analyst_brief: {
      summary_zh: '材料B现价 6,940 元/吨,预计跌至 6,660 元/吨。',
      why_price_zh: '上游裂解原料A近30日-2.8%。',
      why_action_zh: '历史数据不足,建议观望。',
      watch_triggers_zh: ['方向准确率回升至 55% 以上'],
      risk_zh: '近期无重大市场事件。',
      source: 'llm',
    },
  };
  // render expanded row, assert:
  // screen.getByText(/上游裂解原料A/) exists
  // screen.getByText('关注触发') exists
  // screen.getByText(/方向准确率回升至 55%/) exists
  // screen.getByText('AI 分析师撰写') exists
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`
Expected: FAIL — unable to find text 上游裂解原料A

- [ ] **Step 3: Implement the JSX change above**

- [ ] **Step 4: Run test to verify it passes**

Same command. Expected: PASS.

---

### Task 12: Domain-signals A/B validation + E2E rollout

**Files:** none (ops/verification)

- [ ] **Step 1: Run the A/B harness with real Acme data**

```bash
cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_domain_signals_ab.py -v -s
```
If the harness test is synthetic-only, run a real-data evaluation inside the backend container:
```bash
docker exec zhanlu-backend python -c "
from app.services.forecasting.domain_signals_ab import ab_evaluate
from app.services.forecasting.edia_source import EdiaMysqlDataSource
import pandas as pd
src = EdiaMysqlDataSource()
for pid, table in [('product_b','md_market_price'), ('product_d','md_market_price')]:
    df = src.read_history({'table': table, 'product_id': pid})
    # ... walk-forward ab_evaluate per product, print ABResult
"
```
Decision rule (from the harness docstring): ON beats OFF by ≥5% relative MAPE on ≥60% of products → enable.

- [ ] **Step 2: Set flags and restart**

In the repo `.env` (and `app/config.py` defaults stay False):
```
FORECAST_DOMAIN_SIGNALS_ENABLED=true   # only if Step 1 recommends
FORECAST_ANALYST_LLM_ENABLED=true
```
`docker restart zhanlu-backend && sleep 20`

- [ ] **Step 3: Verify decision board end-to-end**

```bash
TOKEN=$(curl -s -X POST http://localhost:5002/api/apps/local-zhanlu-app/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s "http://localhost:5002/api/market-dashboard/decision-board?group=midstream&day=7" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -120
```
Verify per product: `analyst_brief` present, `source` is `llm` (or `template` if verifier rejected — check backend logs for `[analyst]`), `why_price_zh` names upstream + elasticity, `why_action_zh` explains trust reasons, `watch_triggers_zh` non-empty.

- [ ] **Step 4: Verify nightly-run pre-warm path**

Trigger one product forecast and confirm `explanation->'analyst_brief'->'7'` exists on the new run:
```bash
docker exec zhanlu-backend python -c "
from app.database import SessionLocal
from app.services.forecasting.analyst import service as s
db = SessionLocal()
b = s.get_analyst_brief('product_b', 7, db=db)
print(b['source'] if b else 'NONE'); db.close()
"
```

- [ ] **Step 5: Full test sweep + frontend preview**

Run the forecast test files individually:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
for f in test_forecast_trust_reasons test_forecast_analyst_agreement test_forecast_analyst_pack test_forecast_analyst_template test_forecast_analyst_verifier test_forecast_analyst_writer test_forecast_analyst_service test_service_decision_board test_router_decision_board test_forecast_brief_tool test_weekly_report_analyst test_forecast_analyst_prewarm; do venv/bin/python -m pytest tests/$f.py -q || echo "FAILED: $f"; done
```
Then `preview_url` the market dashboard Decision Board for visual confirmation.

---

## Self-Review Notes

- **Spec coverage:** Section 1 signals — domain enablement (T12), price-chain divergence (T3 causal), model agreement (T2), why-low-trust (T1), drivers+seasonal surfacing (T3/T4). Section 2 writer+verifier+cache+fallback (T5/T6). Section 3 surfaces (T7/T8/T9/T11). Section 4 testing/rollout (all + T12).
- **No import cycles:** `analyst/*` imports `forecasting.narrative`, `domain_signals`, `mds`, `llm_service` only. `decision_board_service` → `analyst.service` (not vice versa). `engine` → `analyst.service` (lazy import inside function). `weekly_report` → `analyst.service` (lazy import).
- **Type consistency:** brief dict keys identical across template renderer, writer, verifier, `AnalystBriefMeta`, frontend. `get_analyst_brief(product_id, day, db, persist)` signature consistent in T6/T7/T8/T9/T10 (T7/T8/T9 call without `persist`; T10 via `prewarm_brief`).
- **ERP products** (`product_a`, `feedstock_a` in `_ERP_PRODUCT_KEYS`): their product-level target key exists (`acme.product_a` per PRODUCT_FORECAST_TARGET_KEY) so `_latest_run` works; SKU-level briefs are out of scope (board shows SKU table separately).
