# Spec: Per-Product AI Brief Restructure (Approach B)

**Date**: 2026-08-05
**Status**: Approved → implementing
**Scope**: Per-product AI Brief only, using data the system already has (no new data sources)

## Goal

Restructure the per-product AI Brief from 5 free-form fields (summary/why_price/why_action/triggers/risk) into 7 section-based fields that mirror the human-written weekly reports 1:1. The highest-value gap: forecast **ranges** (bear–bull) + **directional signal** (arrows + qualitative phrases like 易跌难涨), which the evidence pack already computes but the current template discards.

## New schema — `AnalystBriefMeta`

```python
class AnalystBriefMeta(BaseModel):
    market_update_zh:   str = ""           # 市场动态 — intel events + volatility
    price_data_zh:      str = ""           # 价格数据 — current + percentile + trend
    upstream_logic_zh:  str = ""           # 上游传导 — causal chain
    supply_demand_zh:   str = ""           # 供需研判 — seasonal + model drivers
    forecast_zh:        str = ""           # 价格预测 — range + direction + confidence
    watch_triggers_zh:  list[str] = []     # 触发条件 — re-eval triggers
    risk_zh:            str = ""           # 风险提示 — divergence/spread/vol
    source: str = "template"               # "llm" | "template"
    day: Optional[int] = None
    generated_at: Optional[str] = None
```

Old fields (`summary_zh`, `why_price_zh`, `why_action_zh`) **removed** — clean break. Cached briefs regenerate on next access (old cache miss → rebuild).

## Directional signal logic (new helper)

```python
def _directional_signal(p: dict) -> tuple[str, str]:
    chg = p.get("expected_change_pct") or p.get("implied_change_pct")
    spread = p.get("spread_pct") or 0
    if chg is None: return ("?", "方向不明")
    if abs(chg) < 0.015 and spread > 0.04: return ("↔", "震荡")
    if chg >= 0.02:  return ("↗", "偏强")
    if chg <= -0.02: return ("↘", "偏弱")
    return ("→", "稳定")

def _qualitative_tail(p: dict) -> str:
    chg = p.get("expected_change_pct"); p_rise = p.get("p_rise")
    if chg is not None and p_rise is not None:
        if chg < 0 and p_rise < 0.45: return "易跌难涨"
        if chg > 0 and p_rise > 0.55: return "易涨难跌"
    return ""
```

Thresholds (0.02 / 0.015 / 0.04) sit *below* the decision engine's `_MIN_CHG=0.03` so the directional signal is more sensitive than the buy/sell action gate.

## Section → evidence-pack field mapping

| Section | Renderer | Source fields |
|---|---|---|
| 市场动态 | `_market_update(p)` | `intelligence.*`, `policy.volatility_regime` |
| 价格数据 | `_price_data(p)` | `current_price`, `price_percentile`, `trend.*` |
| 上游传导 | `_upstream_logic(p)` | `upstream[0].*`, `causal.*` |
| 供需研判 | `_supply_demand(p)` | `seasonal.*`, `drivers[]`, `models.agreement` |
| 价格预测 | `_forecast(p)` | `forecast_base`, `bull`, `bear`, `spread_pct`, `expected_change_pct`, `p_rise`, `decision.*` |
| 触发条件 | `_triggers(p)` | (kept as-is, returns list) |
| 风险提示 | `_risk(p)` | (kept as-is) |

## Files changed

**Backend source (6):**
1. `app/schemas/market_dashboard.py` — `AnalystBriefMeta` 7 fields
2. `app/services/forecasting/analyst/template_brief.py` — 7 renderers + `_directional_signal` + `_qualitative_tail`
3. `app/services/forecasting/analyst/verifier.py` — `REQUIRED_KEYS` + `texts` updated to 7 fields
4. `app/services/forecasting/analyst/brief_writer.py` — `BRIEF_JSON_SCHEMA` + prompt rewritten
5. `app/services/decision_board_service.py` — `narrative_zh` joins 7 sections with titles; `AnalystBriefMeta` construction
6. `app/services/weekly_report/generator.py` — narrative concatenation updated

**Backend tests (7):** test_forecast_analyst_template, _verifier, _writer, _service, test_service_decision_board, test_forecast_brief_tool, test_weekly_report_analyst

**Frontend (2):**
7. `DecisionBoard.jsx` — render 7 sections with Chinese titles; forecast section gets emphasis
8. `DecisionBoard.test.jsx` — fixture updated

## Out of scope (would need new data sources)
- Competitor prices, regional price ladder, downstream operating rates, named supply events.
