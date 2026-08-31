"""Evidence pack assembly for the forecast analyst (pure builders)."""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd  # noqa: F401  (type context for forecast series)

from app.services.domain_config import get_domain_config
from app.services.forecasting import narrative as fn
from app.services.forecasting.domain_signals import (
    compute_seasonal_adjustment, get_elasticity,
)

# Supply-chain map (downstream product → upstream product ids) — loaded
# from the app's domain config ("upstream_map" key). Empty config = empty
# map: no upstream evidence, and callers degrade gracefully (unknown
# product group, no upstream series).
UPSTREAM_MAP: dict[str, list[str]] = dict(
    (get_domain_config("") or {}).get("upstream_map") or {}
)

# Display labels for upstream products — from the app's domain config
# ("product_labels" block). Absent labels fall back to the product id.
_PRODUCT_LABELS: dict[str, str] = dict(
    (get_domain_config("") or {}).get("product_labels") or {}
)


def _get_product_group(product_id: str) -> str:
    """Classify a product as upstream/midstream/downstream.

    ``UPSTREAM_MAP`` keys are downstream products; their upstream values
    form the midstream set. Anything else is unknown (the market
    dashboard group map was removed with that feature).
    """
    for _down, ups in UPSTREAM_MAP.items():
        if product_id in ups:
            return "upstream"
    if product_id in UPSTREAM_MAP:
        return "downstream"
    return "unknown"


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


def compute_price_percentile(history_rows: list) -> Optional[float]:
    """Percent of history below the last price (1 decimal); None if <10 points."""
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
               run_results, model_detail, explanation, as_of_month,
               demand_signal: dict | None = None,
               supplier_ladder: dict | None = None,
               downstream_utilization: dict | None = None,
               inventory_pressure: dict | None = None,
               import_pressure: dict | None = None) -> dict:
    """Assemble the full evidence pack (pure — all data passed in)."""
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
            "name_zh": _PRODUCT_LABELS.get(up_id, up_id),
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

    trend_self = fn.compute_trend_stats(history_rows)

    current_price = trend_self.get("last_price")
    forecast_base = hs["first"]
    implied_change_pct = None
    if current_price and forecast_base is not None:
        implied_change_pct = round(forecast_base / current_price - 1.0, 4)

    return {
        "product_id": product_id, "name_zh": name_zh, "day": day,
        "product_group": _get_product_group(product_id),
        "current_price": current_price,
        "price_date": (history_rows[-1][0] if history_rows else None),
        "price_percentile": compute_price_percentile(history_rows),
        "forecast_base": forecast_base,
        "forecast_end": hs["unc"].get("base"),
        "bull": hs["unc"].get("bull"), "bear": hs["unc"].get("bear"),
        "spread_pct": hs["unc"].get("spread_pct"),
        "expected_change_pct": exp_pct, "p_rise": prob.get("p_rise"),
        "implied_change_pct": implied_change_pct,
        "trend": trend_self,
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
        # Decision-engine constants the narrative may legitimately reference
        # (mirrors decision_engine.py / narrative.py thresholds).
        "thresholds": {"buy_p": 0.70, "sell_p": 0.30, "min_change": 0.03,
                       "edge_accuracy": 0.55, "trend_window_days": 30},
        # Phase F1: demand signal + supplier ladder (optional, zero regression risk)
        "demand": demand_signal or {},
        "supplier_ladder": supplier_ladder or {},
        # Wave 3 T3.5: external-feed signals (optional, zero regression risk)
        "downstream_utilization": downstream_utilization or {},
        "inventory_pressure": inventory_pressure or {},
        "import_pressure": import_pressure or {},
    }
