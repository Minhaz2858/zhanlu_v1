# EDIA Overview Dashboard — Verbatim Port to Zhanlu

**Date**: 2026-07-31
**Status**: Draft → User Review
**Author**: AI pair (brainstorming session with user)

## Goal

Replicate the EDIA `OverviewDashboard.tsx` UI/UX in zhanlu **verbatim** — same 3-column price grid with regional SKUs, same 7-day forecast section, same Industry Intelligence news section. Read data from EDIA's MySQL warehouse directly (no AI pipeline replication).

## Why Verbatim

User wants the EXACT look-and-feel from `/home/ysk2025/EDIA_5.1.2/frontend/src/components/OverviewDashboard.tsx` (2076 lines). Skipping this would be an interpretation; user prefers the real port.

## Architecture

```
┌─ Frontend ────────────────────────────────────────────────────────┐
│ MarketDashboardPage.jsx  (verbatim port of OverviewDashboard.tsx)  │
│   ├─ fetchWithRetry → authFetch                                   │
│   ├─ API_BASE_URL   → /api/market-dashboard                       │
│   └─ TYPO / NO_DATA_LABEL / getPriceChangeTextClass               │
│       → existing ported lib files in                              │
│         frontend/src/pages/MarketDashboard/lib/* (from Phase B3)  │
└─────────────────────────────────┬─────────────────────────────────┘
                                  │ HTTPS
┌─ Backend (FastAPI) ────────────▼─────────────────────────────────┐
│ /api/market-dashboard/                                            │
│   ├─ /snapshot              ✓ (existing)                         │
│   ├─ /regional/{id}         ✓ (existing — single product)        │
│   ├─ /regional-batch        NEW — batch version (Overview uses)  │
│   ├─ /forecast-base-batch   NEW — reads v_forecast_price          │
│   ├─ /events/latest         NEW — reads intelligence_events       │
│   └─ /fx/usd-cny            NEW — static rate (~7.2 CNY/USD)      │
└─────────────────────────────────┬─────────────────────────────────┘
                                  │ MySQL
┌─ EDIA warehouse ───────────────▼─────────────────────────────────┐
│ aipdp_data_warehouse_prod @ 10.10.10.49:3306                     │
│   ├─ 裂解C5 / 异戊二烯 / etc. ✓ (already reading)                 │
│   ├─ v_forecast_price   (NEW read)                                │
│   └─ intelligence_events (NEW read — 5994 approved rows)          │
└───────────────────────────────────────────────────────────────────┘
```

## Data Mapping

### Product → Material (forecast table)

| product_id | material_name (Chinese) | material_id | Has forecast? |
|---|---|---|---|
| crude_oil | — (not in v_forecast_price) | — | ✗ (show "—") |
| naphtha | — | — | ✗ (show "—") |
| cracked_c5 | 工业用裂解碳五 / 裂解碳五 | 103319/103320/103321/103322 | ✓ |
| cracked_c9 | 工业用裂解碳九 / 裂解碳九 / 碳九原料 | 103323/103324/103325/103326 | ✓ |
| isoprene | — | — | ✗ |
| piperylene | — | — | ✗ |
| dcpd | — | — | ✗ |
| raffinate_c5 | — | — | ✗ |
| sis | — | — | ✗ |
| c5_resin | — | — | ✗ |
| blowing_agent | — | — | ✗ |
| styrene | — | — | ✗ |

Missing forecast → return `null` from the endpoint → OverviewDashboard renders "—" (matches EDIA behavior).

### Intelligence events (news)

Read directly from `intelligence_events` MySQL table. Schema matches EDIA's response shape 1:1 — no transformation needed.

| EDIA field | MySQL column |
|---|---|
| event_id | event_id |
| event_type | event_type |
| headline | headline |
| affected_commodities | affected_commodities (JSON text) |
| direction | direction |
| magnitude_estimate | magnitude_estimate |
| certainty | certainty |
| time_horizon | time_horizon |
| geographic_scope | geographic_scope |
| relevance_to_c5_c9 | relevance_to_c5_c9 |
| key_entities | key_entities (JSON text) |
| causal_chain_hint | causal_chain_hint |
| source_url | source_url |
| source_credibility | source_credibility |
| full_text | full_text |
| raw_quote | raw_quote |
| detected_at | detected_at |
| impact_magnitude | impact_magnitude |
| relevance_score | relevance_score |
| review_status | review_status |
| usefulness_score | usefulness_score |

## Backend Endpoints (NEW)

### `GET /api/market-dashboard/regional-batch?product_ids=...`

Batch version of existing `/regional/{id}`. EDIA calls this 3 products at a time (chunks REGIONAL_BATCH_CHUNK_SIZE=3 in OverviewDashboard).

- **Params**: `product_ids` — comma-separated, e.g. `crude_oil,naphtha,cracked_c5`
- **Response shape** (matches EDIA's RegionalBatchResponse):
  ```json
  {
    "items": {
      "crude_oil": { "product_id": "crude_oil", "regions": [...], "spread_cny": null, ... },
      "naphtha":   { "product_id": "naphtha",   "regions": [...], "spread_cny": null, ... }
    },
    "errors": { "broken_id": "error message" }
  }
  ```
- **Implementation**: reuse `get_regional_prices()` per product, group into `items` map, timeout 60s per chunk (matches EDIA's OVERVIEW_REGIONAL_LOAD_TIMEOUT_MS)

### `GET /api/market-dashboard/forecast-base-batch?product_ids=...&day=7`

7-day forecast base prices (matches EDIA's `/api/market/forecast-base-batch`).

- **Params**: `product_ids`, `day=7` (1-30)
- **Response shape**:
  ```json
  {
    "day": 7,
    "base_by_product": {
      "cracked_c5": 6824.0,
      "crude_oil": null,
      ...
    }
  }
  ```
- **Implementation**: read `v_forecast_price` for the matching material_ids, get the price where `date = (max_forecast_date + (day-1) days)`. Return null for products with no forecast data.

### `GET /api/market-dashboard/events/latest?hours=168&review_status=approved&min_usefulness_score=0`

News/events feed (matches EDIA's `/api/events/latest`).

- **Params**: `hours=168` (7 days), `review_status=approved`, `min_usefulness_score=0`
- **Response shape**:
  ```json
  {
    "events": [
      { "event_id": "...", "headline": "...", "direction": "...", ... },
      ...
    ],
    "count": 12
  }
  ```
- **Implementation**: `SELECT * FROM intelligence_events WHERE review_status=? AND usefulness_score >= ? AND detected_at >= NOW() - INTERVAL ? HOUR ORDER BY detected_at DESC LIMIT 100`. Map each row 1:1 to the EDIA response shape (with `_safe_json_list()` for the JSON-text columns).

### `GET /api/market-dashboard/fx/usd-cny`

USD-CNY FX rate (matches EDIA's `/api/v2/market/fx/usd-cny`).

- **Response shape**:
  ```json
  {
    "price": 7.2,
    "unit": "CNY/USD",
    "as_of_date": "2026-07-31",
    "series_product_id": "usd_cny",
    "series_price_type": "spot",
    "data_source": "static_fallback",
    "available": true
  }
  ```
- **Implementation**: static `7.20` for v1. v2 can read from a market data source. The OverviewDashboard only uses this for display; the missing rate doesn't break anything.

## Frontend Changes

### New file: `MarketDashboardPage.jsx` (~2000 lines after TS→JS conversion)

**Conversion steps**:
1. Read `/home/ysk2025/EDIA_5.1.2/frontend/src/components/OverviewDashboard.tsx` line by line
2. Strip all `: Type` annotations, `interface XProps {}` blocks, `as Type` casts
3. Replace `import { API_BASE_URL, fetchWithRetry } from "../services/api"` → `import { authFetch } from "@/api/authFetch"`
4. Replace `import { TYPO } from "../lib/typography"` → `import { TYPO } from "./lib/typography"` (already ported)
5. Replace `import { NO_DATA_LABEL, getPriceChangeTextClass } from "../lib/priceChange"` → same path, already ported
6. Replace `fetchWithRetry(\`${API_BASE_URL}/api/events/latest?...\`)` → `authFetch('/api/market-dashboard/events/latest?...')`
7. Same for forecast, regional-batch, fx
8. Replace `import React, { ... } from "react"` → `import React, { ... } from "react"`
9. Strip TypeScript type assertions like `as "en" | "zh"`, `as MarketSnapshotItem[]`, etc.
10. Strip `interface EventItem {}`, `interface ForecastResponseLite {}` etc. (just remove them; types are erased at runtime)
11. The `MarketSnapshotItem` shape coming in as a prop must match our existing snapshot API response — verify field names match (they do: `id`, `name`, `price`, `change_pct`, `weekly_change_pct`, `data_source_detail`, etc.)

**Helpers to port** (functions defined at the top of OverviewDashboard.tsx):
- `chunkArray` — small utility, inline port
- `readOverviewSessionCache` / `writeOverviewSessionCache` — session storage wrapper, port
- `qualityScore`, `isProfessionalNews`, `deriveHeadline`, `deriveSummary` — news scoring helpers
- `inferDirectionFromText`, `weeklyImpactSignal`, `weeklyImpactTone` — direction inference
- `sortByNewsPriority`, `dedupeByHeadline`, `normalizeEvent`, `selectUsefulNews`, `classifyNewsColumn`, `isRelatedNews` — news filtering
- `formatDateLabel`, `formatShortDate`, `formatAbsoluteChange`, `formatNewsTimestamp`, `dateTextClass` — date/format helpers
- `formatPriceValue`, `changeColor`, `changeSignal`, `resolveGap` — price display helpers
- `REGIONAL_LABEL_EN`, `REGIONAL_LABEL_ZH` — region name translations
- `HIDDEN_REGIONAL_PRICE_TYPES`, `BAD_NEWS_PATTERNS`, `RELATED_NEWS_TOKENS` — constants
- `TextBundle` and `i18n` text strings — keep EN/ZH text inline

**Snapshot prop**: OverviewDashboard receives `snapshot: MarketSnapshotItem[]` as a prop from parent. In zhanlu, the parent is the route component that fetches via `marketDashboardApi.snapshot()`. The shape must include all the fields the OverviewDashboard reads. Our existing snapshot has all of them (`id`, `name`, `price`, `change_pct`, `weekly_change_pct`, `daily_gap_value`, `weekly_gap_value`, `data_source_detail`, etc.) — no changes needed.

### Modified: `marketDashboard.js`

Add 4 new methods to match OverviewDashboard's API calls:

```js
export const marketDashboardApi = {
  enabled: ...,
  snapshot: ...,
  history: ...,
  regional: (productId) => getJson(`/api/market-dashboard/regional/${productId}`),
  regionalBatch: (productIds) => 
    getJson('/api/market-dashboard/regional-batch', { product_ids: productIds.join(',') }),
  forecastBaseBatch: (productIds, day = 7) =>
    getJson('/api/market-dashboard/forecast-base-batch', { product_ids: productIds.join(','), day }),
  events: (hours = 168, reviewStatus = 'approved', minUsefulnessScore = 0) =>
    getJson('/api/market-dashboard/events/latest', { hours, review_status: reviewStatus, min_usefulness_score: minUsefulnessScore }),
  fxRate: () => getJson('/api/market-dashboard/fx/usd-cny'),
  news: (group, limit = 18) => getJson('/api/market-dashboard/news', { group, limit }),  // existing
};
```

The existing `news()` method (which returns the v1 stub) can stay for backward compat — OverviewDashboard doesn't use it (it uses `events()` instead).

### Removed/replaced: existing `components/SnapshotGrid`, `PriceChart`, `RegionalTable`, `NewsPanel`

The OverviewDashboard renders its own UI inline (it doesn't use these components). The existing components were v1 scaffolding and will be removed or kept as no-op exports to avoid breaking other tests.

Actually, per the verbatim principle, **keep them as-is for now** (don't delete code that doesn't break anything). The page will use the OverviewDashboard's inline UI.

## Tests

### Backend (pytest, TDD)
- `test_regional_batch_endpoint.py` — happy path, error path, chunking
- `test_forecast_base_batch_endpoint.py` — day parameter, missing product returns null
- `test_events_latest_endpoint.py` — hours filter, review_status filter, JSON parsing
- `test_fx_endpoint.py` — returns valid response shape

### Frontend (vitest, TDD)
- `marketDashboard.test.js` — extend with regionalBatch, forecastBaseBatch, events, fxRate
- `MarketDashboardPage.test.jsx` — smoke test: renders without error, shows "EDIA Market Dashboard — overview" title, calls the 4 new API methods

The full OverviewDashboard unit testing is out of scope (we trust the port; the smoke test catches structural errors).

## Out of Scope (v1)

- **Live FX rate** — static 7.20 for v1 (USD-CNY is stable; user can manually update if needed)
- **Product catalogs / SKU explorer view** — EDIA's `Overview (SKU)` view not in this port
- **Upstream/Midstream/Downstream sub-views** — EDIA's detailed views (PriceChain, PricingActions, ProductComparison, InventorySafety) not in this port; future phases
- **Execution prices** — EDIA's "执行价" rows in the regional price section depend on zhanlu's ERP integration (not part of v1 mirror)
- **i18n beyond EN/ZH** — keep EDIA's English/Chinese text as-is

## Phase Decomposition

This is a single focused phase. Future phases can add:
- Phase 2: Upstream Feedstocks sub-view (PriceChain.tsx)
- Phase 3: Midstream Products sub-view (PricingActions.tsx)
- Phase 4: Downstream Products sub-view
- Phase 5: Product Comparison view
- Phase 6: Inventory Safety Warning view

## Risks

1. **TS→JS conversion errors** — the file is 2076 lines with many type annotations. Mitigation: port section by section, compile after each, keep the file in working state.
2. **Field name mismatches** between EDIA's snapshot shape and our existing snapshot API. Mitigation: verify the shapes match during the port, add a normalization layer if needed.
3. **Missing data** (forecast for isoprene/piperylene/etc.) — UI shows "—" which matches EDIA's behavior. No risk.
4. **Large bundle size** — adding ~2000 lines to a single page component. Mitigation: keep existing code-splitting; the new page is lazy-loaded via the `/market-dashboard/:section` route.

### Snapshot field requirements (self-review)

The OverviewDashboard reads these fields from the `MarketSnapshotItem` (passed as the `snapshot` prop):

| Field | Used for | Our current API returns? |
|---|---|---|
| `id` | routing, table keys | ✓ |
| `name` | display label | ✓ |
| `price` | "Latest Price" display | ✓ |
| `change_pct` | daily change in table | ✓ |
| `weekly_change_pct` | "Weekly Change" in dark sidebar | ✓ (Phase A1 mapping) |
| `weekly_gap_value` | dark sidebar (single number) | ✓ |
| `data_source_detail` | tooltip on product name | ✓ |
| `note` | "—" for unmapped products | ✓ |
| `execution_price` | optional column in regional table | ✗ — return null; UI shows "—" |
| `execution_price_change` | optional column | ✗ — return null; UI shows "—" |
| `execution_price_date` | optional column | ✗ — return null; UI shows "—" |
| `execution_price_prev_date` | optional column | ✗ — return null; UI shows "—" |

The 4 `execution_price*` fields come from EDIA's ERP integration (the actual executed trade price), not from the MySQL warehouse mirror. They will display as "—" / blank in the port — this matches EDIA's behavior for products without recent execution data. Documented in the inline `data_source_detail` tooltip so users understand the difference.

No changes needed to our existing snapshot service — all required fields are already returned.

### Regional field requirements (self-review)

The OverviewDashboard's regional price table reads from `regional.regions[]` (returned by `/api/market-dashboard/regional-batch`):

| Field | Our endpoint returns? |
|---|---|
| `price_type` (label) | ✓ |
| `is_internal` | ✓ |
| `price` | ✓ |
| `change_pct` | null (we don't compute daily change for regional) |
| `date` | ✓ |
| `days_stale` | ✓ (always 0 in v1) |

Optional fields read by OverviewDashboard that we'll return `null`:
- `spread_cny` (cost-floor spread analysis) — out of scope, null
- `cost_floor_price` — out of scope, null
- `cost_floor_note` — out of scope, null

These will show as "—" in the port.

## Implementation Order

1. Backend Phase A: Add 4 new endpoints + tests (5 backend tasks)
2. Frontend Phase B: Extend marketDashboard.js API client (1 task)
3. Frontend Phase C: Port OverviewDashboard.tsx → MarketDashboardPage.jsx (1 large task — done in one commit)
4. Frontend Phase D: Replace MarketDashboardPage with the port, remove scaffolding (SnapshotGrid, PriceChart, etc.) if no longer used (1 task)
5. Manual smoke test: verify all 3 sections render with real data, hard refresh, take screenshot
