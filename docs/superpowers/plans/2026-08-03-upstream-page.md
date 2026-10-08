# Upstream Page Implementation Plan

## Tasks

### Task 0: Fix ForecastRun key `"30d"` → `"30"`
- File: `backend/app/services/market_dashboard_service.py`, line ~890
- Change `"30d"` → `"30"` in the forecast run lookup

### Task 1: `GET /forecast-chart/{product_id}` endpoint
- Backend: schema, service method, router
- Returns: actual prices (past 30d), previous AI recommendations, forecast (base/bull/bear for next 30d)

### Task 2: `GET /price-chain` endpoint
- Backend: schema, service method, router
- Returns: 3-tier price transmission chain data (crude → product_c → cracked → derivatives)

### Task 3: API client functions
- File: `frontend/src/api/marketDashboard.js`
- Add `fetchForecastChart(productId, days)` and `fetchPriceChain()`

### Task 4: ObservedDataStrip component
- New file: `frontend/src/pages/MarketDashboard/components/ObservedDataStrip.jsx`
- Horizontal product selector strip

### Task 5: ForecastTrendChart component
- New file: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`
- Composed chart with past/forecast zones, rich tooltip

### Task 6: PriceTransmissionChain component
- New file: `frontend/src/pages/MarketDashboard/components/PriceTransmissionChain.jsx`
- 3-tier horizontal flow with SVG arrows, sparklines, node cards

### Task 7: Page integration
- File: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Wire all 3 new components into the "upstream" section
