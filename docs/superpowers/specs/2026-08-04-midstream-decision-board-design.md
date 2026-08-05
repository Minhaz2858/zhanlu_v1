# Midstream Decision Board — Design Spec

**Date**: 2026-08-04
**Page**: `/market-dashboard/midstream` (reusable for upstream/downstream)
**Approach**: A+B hybrid — decision matrix table (A) + expandable rows revealing a large card with mini chart & full AI narrative (B)

## Problem

The midstream page forces users to click each of the 4 products one-by-one and hover tooltips to compare decisions. The backend already computes everything (`/forecast-base-batch`: forecast base, P(rise), expected change %, directional edge, decision action/confidence/rationale, trust tier, intel events, volatility regime) but the page never calls it. A CEO→sales audience needs: **all materials at once, name + code, forecast, and a plain-language AI recommendation for next week.**

## Audience & Language

- CEO, business, marketing, sales — non-technical.
- Narratives in **Chinese plain language (大白话)**; keep existing bilingual labels.
- Avoid jargon in UI: no "P(rise)", "directional edge", "trust tier" as raw terms — translate to 上涨概率 / 信号可靠度 etc.

## Backend

### New endpoint

`GET /api/market-dashboard/decision-board?group=midstream&day=7`

Response:

```json
{
  "group": "midstream",
  "day": 7,
  "generated_at": "2026-08-04T08:00:00",
  "briefing_zh": "下周重点关注:异戊二烯看涨(上涨概率 68%),建议提前备货;双环戊二烯信号不明,建议观望。…",
  "products": [
    {
      "id": "isoprene",
      "name_zh": "异戊二烯",
      "name_en": "Isoprene",
      "code": "isoprene",
      "current_price": 12100.0,
      "price_date": "2026-08-03",
      "weekly_change_pct": 1.8,
      "forecast_base": 12450.0,
      "expected_change_pct": 0.029,
      "p_rise": 0.68,
      "action": "buy",
      "action_label_zh": "建议备货",
      "confidence": "medium",
      "trust_tier": "high",
      "trust_label_zh": "高可信",
      "trust_color": "green",
      "directional_accuracy": 0.72,
      "directional_status": "edge",
      "intel_event_count": 3,
      "intel_bias": "up",
      "volatility_regime": "NORMAL",
      "one_liner_zh": "供应偏紧,上涨概率 68%,建议提前备货",
      "narrative_zh": "异戊二烯现价 12,100 元/吨,预计下周涨至 12,450 元/吨(+2.9%),上涨概率 68%。方向预测历史准确率 72%,市场情报偏多(3 条在册事件)。建议:提前备货,锁定当前价格。",
      "skus": []
    }
  ]
}
```

- `skus` populated for ERP products (raffinate_c5) from existing `sku_forecasts_by_product` (material_code + name + forecast_base).
- Missing forecast/decision data → fields `null`, `action="watch"`, `one_liner_zh="数据不足,建议观望"` — never crash.

### New service module: `backend/app/services/decision_board_service.py`

- `build_decision_board(group, day, db)` — orchestrates:
  - `get_market_snapshot(group)` for current prices (reuse, no new MySQL queries).
  - `get_forecast_base_batch_v2(product_ids, day, db)` for all overlay data (reuse).
- Pure template functions (unit-testable, no LLM):
  - `action_label(action)` → buy=建议备货 / sell=建议出货 / hold=按需跟进 / watch=建议观望
  - `build_one_liner(...)` → single sentence
  - `build_narrative(...)` → 2-3 sentences weaving price, forecast %, p_rise, directional accuracy, intel count/bias, volatility
  - `build_briefing(products)` → cross-product weekly summary, highlighting strongest buy/sell signals first
- Narrative templates zh-primary; numbers formatted with thousands separators, percentages as integers.

### Router

Add route in `backend/app/routers/market_dashboard.py` (same `get_current_user_required` guard). Reuse EDIA-enabled fail-closed pattern (503 `edia_unavailable`).

## Frontend

### New component: `pages/MarketDashboard/components/DecisionBoard.jsx`

Layout (inserted between `ObservedDataStrip` and `ForecastTrendChart`):

1. **Briefing card** — sparkles icon + `briefing_zh`, subtle gradient background.
2. **Matrix table** — one row per product:

| 物料 | 现价 | 7日预测 | 预期涨跌 | 上涨概率 | AI建议 | 可靠度 | AI 简评 | ▾ |
|---|---|---|---|---|---|---|---|---|

- 物料: zh name (bold) + `code` mono badge below.
- 预期涨跌: colored +/- % (red up / green down per Chinese market convention — confirm: page currently uses red=up).
- AI建议 badge: 建议备货(green) / 建议出货(red) / 按需跟进(blue) / 建议观望(amber).
- 可靠度: colored dot + label (高可信 green / 中等 yellow / 方向性 orange / 低 red).
- Row click → `onSelectProduct(id)` (syncs ForecastTrendChart + RegionalTable below).
- Chevron click → expand row.

3. **Expanded card** (B-part, inline below the row, full width):
   - Left: mini chart (recharts `AreaChart`, past 30d actual + 7d base forecast) via existing `marketDashboardApi.forecastChart(id, 30)`, lazy-fetched only when expanded.
   - Right: full `narrative_zh` paragraph + stats grid (方向准确率, 情报事件数, 波动状态, 置信度).
   - SKU table for raffinate_c5 (material_code, name, forecast_base).
   - Only one row expanded at a time.

### Wiring: `MarketDashboardPage.jsx`

- Add `decisionBoardQuery` (`useQuery`, keyed by section, `enabled` same as snapshot).
- Render `<DecisionBoard group={section} data onSelectProduct={setSelectedProductId} selectedId />` for product sections after `ObservedDataStrip`.
- Graceful: query error → hide board, keep existing page (board is additive).

### API client: `api/marketDashboard.js`

Add `decisionBoard: (group, day = 7) => getJson('/api/market-dashboard/decision-board', { group, day })`.

## Styling

- Follow existing zhanlu monochrome theme + `TYPO` tokens; support dark mode (`useDarkMode` pattern from ForecastTrendChart).
- Chinese market color convention: 涨=red, 跌=green for price moves; action badges use semantic colors (备货=green, 出货=red).
- No emojis; lucide icons only (Sparkles, TrendingUp/Down, ChevronDown, Package, Eye).

## Testing

- **Backend** `tests/test_decision_board_service.py`: template functions (action labels, one-liner, narrative, briefing), null-data fallbacks, endpoint integration (mock snapshot + batch overlay).
- **Frontend** `DecisionBoard.test.jsx` (vitest, happy-dom): renders 4 rows from fixture, badge labels correct, expand toggles, row click fires onSelectProduct.
- Run backend tests individually (SSH RAM limited); restart `zhanlu-backend` container after service changes; verify endpoint with curl against port 5002.

## Out of scope

- LLM-polished narratives (template now; LLM upgrade later behind flag).
- Day selector (fixed 7d "next week"; 3/30 selectable later).
- PDF/export of the board.
