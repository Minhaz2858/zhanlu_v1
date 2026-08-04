# Design Spec: EDIA Market Dashboard Integration into zhanlu_7_30

**Date**: 2026-07-31
**Status**: Approved (user-confirmed assumptions on 2026-07-31)
**Author**: CodeBuddy (via brainstorming skill)
**Source of parity**: `/home/ysk2025/EDIA_5.1.2/` (EDIA reference implementation)

---

## 1. Confirmed Decisions (from user Q&A)

- **Placement**: separate sidebar section (collapsible accordion above the session list)
- **Data access**: direct MySQL connection to EDIA's MySQL DB at `10.10.10.49:3306/aipdp_data_warehouse_prod`
- **Scope**: full EDIA parity for 4 sections — Overview, Upstream Feedback, Midstream Products, Downstream Products

## 2. Confirmed Assumptions (user said "ok do it please" on 2026-07-31)

1. **MySQL credentials** → read from a new env var `EDIA_MYSQL_URL` (e.g. `mysql+pymysql://root:PWD@10.10.10.49:3306/aipdp_data_warehouse_prod`). User adds it to `backend/.env`. Mirrors EDIA's own `DATABASE_URL` pattern and keeps the secret out of git. (Empty value = dashboard routes return 503, the rest of zhanlu unaffected.)

2. **Sidebar visual style** → match existing `linkClass` styling (icon + text, same `bg-sidebar-accent` active state). The accordion header is a "Dashboard" group with a rotating `ChevronDown`. Sub-items reuse `NavLink` + `linkClass` but indent `pl-8` to signal hierarchy.

3. **Active state** → yes, the active sub-item is highlighted via NavLink's `isActive`, AND the accordion auto-expands when `pathname.startsWith('/market-dashboard')`. Manual expand/collapse state is preserved when not on a dashboard route.

## 3. Architecture

```
[Browser]                                         [Backend FastAPI]
  Sidebar                                           app/main.py
  ├─ + New Task                                     ├─ EDIA_MYSQL_URL → core/mysql_db.py
  ├─ nav (Automation, MySpace, …)                   ├─ routers/market_dashboard.py
  ├─ Dashboard accordion  ← NEW                         ├─ GET /api/market-dashboard/enabled
  │   ├─ Overview        → /market-dashboard/overview    ├─ GET /api/market-dashboard/snapshot?group=…
  │   ├─ Upstream        → /market-dashboard/upstream    ├─ GET /api/market-dashboard/history/{id}
  │   ├─ Midstream       → /market-dashboard/midstream    ├─ GET /api/market-dashboard/regional/{id}
  │   └─ Downstream      → /market-dashboard/downstream    └─ GET /api/market-dashboard/news?group=…
  └─ SessionList
                                                   services/market_dashboard_service.py
[Router App.jsx]                                        └─ wraps SQL, returns Pydantic dicts
  /market-dashboard/:section → MarketDashboardPage
                                                   models/market_dashboard.py
[frontend/src/pages/MarketDashboard/]                  └─ read-only Table objs (autoload)
  MarketDashboardPage.jsx
  lib/ (chartTheme, priceChange, typography — ported from EDIA)
  components/ (SnapshotGrid, PriceChart, RegionalTable, NewsPanel)
  api/marketDashboard.js
```

## 4. Backend Changes

### 4.1 Files to Create

- **`backend/app/core/mysql_db.py`** — MySQL engine + SessionLocal for EDIA DB. Pattern copied from EDIA's `backend/src/db/engine_factory.py` (`pool_pre_ping=True`, `pool_size=5`, `connect_args={"connect_timeout": 3}`). Exposes `get_mysql_db()` FastAPI dependency.

- **`backend/app/models/market_dashboard.py`** — SQLAlchemy `Table` objects mapped to EDIA's existing tables using `autoload_with=engine` (read-only, no ORM mutations, no `Base.metadata.create_all`). Confirm exact table/column names from EDIA's `backend/src/db/market_db.py` during implementation.

- **`backend/app/schemas/market_dashboard.py`** — Pydantic response shapes mirroring EDIA's frontend `MarketSnapshot`, `RegionalPriceData`, `PriceHistoryPoint`, `NewsEvent` interfaces (visible in `OverviewDashboard.tsx` lines 6–105).

- **`backend/app/services/market_dashboard_service.py`** — query functions:
  - `get_market_snapshot(db, group: str | None) -> list[MarketSnapshot]` — computes `change_pct` from the last two history points of each product (simpler than EDIA's full `_compute_market_snapshot` which depends on ERP sales data + execution prices not present in our read-only mirror).
  - `get_price_history(db, product_id: str, days: int = 30) -> list[PriceHistoryPoint]`
  - `get_regional_prices(db, product_id: str) -> RegionalPriceData` — **v1 simplified**: returns price per region column (e.g. 华东/华南/华北) without spread/cost-floor analysis (those require EDIA's naphtha cost model, out of scope).
  - `get_news_events(db, group: str | None, limit: int = 18) -> NewsListResponse` — **v1 returns empty list with note**. EDIA sources news from `/api/events/latest` (an AI-detected market-event pipeline stored in a different DB), not a SQL table in the `aipdp_data_warehouse_prod` warehouse. Replicating that pipeline is out of scope for v1; the frontend shows an info banner ("Industry news requires EDIA's AI event pipeline — not part of this v1 read-only mirror.") in place of news cards.
  - `is_edia_enabled() -> bool` — returns `bool(settings.EDIA_MYSQL_URL)`
  - `is_edia_reachable() -> bool` — runs `SELECT 1` against the EDIA engine; cached 30s. Used by `/enabled` to distinguish "not configured" from "configured but unreachable".

- **`backend/app/routers/market_dashboard.py`** — FastAPI router, prefix `/market-dashboard`, session-auth via `Depends(get_current_user_required)` (same as `dashboards.py` line 8). Fail-closed: if `EDIA_MYSQL_URL` is empty → HTTP 503 with `{"error": "edia_unavailable"}`; if MySQL unreachable → HTTP 503 with `{"error": "edia_unreachable"}` — no fallback, no silent empty data.

### 4.2 Files to Modify

- **`backend/app/config.py`** — add field after line 33 (`DATABASE_URL` block):
  ```python
  # EDIA Market Dashboard — read-only mirror of EDIA's MySQL warehouse.
  # Empty = dashboard routes return 503 (the rest of zhanlu is unaffected).
  EDIA_MYSQL_URL: str = ""
  ```
  No validator needed — empty string is the legitimate "disabled" state.

- **`backend/main.py`** — add router import after line 52 (`hooks_router`) and registration after line 642:
  ```python
  from app.routers.market_dashboard import router as market_dashboard_router
  # ...
  app.include_router(market_dashboard_router, prefix="/api")
  ```
  No MySQL init in `lifespan` — engine is created lazily on first query (mirrors EDIA's `engine_factory.get_engine` `lru_cache` pattern).

- **`backend/.env.example`** — append after the existing `DATABASE_URL` block (line 15):
  ```
  # ===== EDIA Market Dashboard (read-only MySQL mirror) =====
  # Empty = dashboard routes return 503. Fill with the EDIA warehouse DSN
  # to enable the /api/market-dashboard/* endpoints.
  # EDIA_MYSQL_URL=mysql+pymysql://root:PASSWORD@10.10.10.49:3306/aipdp_data_warehouse_prod
  ```

### 4.3 Dependencies

`pymysql>=1.1` (requirements.txt line 33) and `cryptography>=41.0` (line 34) are already installed. **No new packages.**

## 5. Frontend Changes

### 5.1 Files to Create

- **`frontend/src/api/marketDashboard.js`** — API client using the existing `base44` SDK pattern (see `Sidebar.jsx` line 3):
  ```js
  export const marketDashboardApi = {
    enabled:  () => base44.get('/market-dashboard/enabled'),
    snapshot: (group) => base44.get('/market-dashboard/snapshot', { params: { group } }),
    history:  (productId, days = 30) => base44.get(`/market-dashboard/history/${productId}`, { params: { days } }),
    regional: (productId) => base44.get(`/market-dashboard/regional/${productId}`),
    news:     (group, limit = 18) => base44.get('/market-dashboard/news', { params: { group, limit } }),
  };
  ```
  Note: `news()` always returns `{items: [], note: "..."}` in v1 (see §4.1 service). Frontend handles it gracefully.

- **`frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`** — single parameterized page; reads `:section` from URL via `useParams()`. `section === 'overview'` → renders all 3 groups stacked; otherwise renders only that group with full detail. Uses `@tanstack/react-query` (already in `package.json` line 50) for caching.

- **`frontend/src/pages/MarketDashboard/lib/`** — ported from `EDIA_5.1.2/frontend/src/lib/`:
  - `chartTheme.js` (from `chartTheme.ts`)
  - `priceChange.js` (from `priceChange.ts`)
  - `typography.js` (from `typography.ts` — the `TYPO` constant)

- **`frontend/src/pages/MarketDashboard/components/`** — adapted from EDIA components:
  - `SnapshotGrid.jsx` — snapshot cards row (price + change pct + 7d high/low)
  - `PriceChart.jsx` — recharts `ComposedChart` (Area + Line) per product
  - `RegionalTable.jsx` — regional price breakdown
  - `NewsPanel.jsx` — related news events list

### 5.2 Files to Modify

- **`frontend/src/components/Sidebar.jsx`** — insert a `DashboardAccordion` sub-component between the existing `<nav>` (lines 221–228) and the `<SessionList>` block (line 233):
  - Add `LayoutDashboard`, `ChevronDown`, `ChevronRight` to the existing `lucide-react` import on line 6
  - The accordion fetches `/market-dashboard/enabled` on mount; if `false`, it renders nothing (avoids dead nav items in dev environments without EDIA access)
  - Sub-items are `NavLink`s to `/market-dashboard/{overview|upstream|midstream|downstream}` reusing the existing `linkClass` with `pl-8` indent
  - Auto-expand when `pathname.startsWith('/market-dashboard')`

- **`frontend/src/App.jsx`** — add lazy import after line 40 (`DashboardView`) and route after line 102:
  ```jsx
  const MarketDashboardPage = lazy(() => import('@/pages/MarketDashboard/MarketDashboardPage'));
  // ...
  <Route path="/market-dashboard/:section" element={<MarketDashboardPage />} />
  ```
  Route is inside the existing `<ProtectedRoute>` block, so session-auth is enforced.

### 5.3 Dependencies

`recharts@2.15.4` (package.json line 76), `lucide-react@0.475.0` (line 63), `@tanstack/react-query@5.84.1` (line 50) are already installed. **No new packages.**

## 6. Product Group Mapping

Confirmed from EDIA's `OverviewDashboard.tsx` lines 118–134:

- **upstream** → `crude_oil`, `naphtha`, `cracked_c5`, `cracked_c9`
- **midstream** → `isoprene`, `piperylene`, `dcpd`, `raffinate_c5`
- **downstream** → `sis`, `c5_resin`, `blowing_agent`, `styrene`
- **overview** → all 12 products, rendered in 3 stacked groups

## 7. Phased Implementation Order

Execute in 5 PR-sized phases (each independently testable):

1. **Phase A — Backend foundation**: config + `mysql_db.py` + models + schemas + service (snapshot + history only) + router + register in `main.py`. Verify: `curl localhost:5002/api/market-dashboard/snapshot?group=upstream`.
2. **Phase B — Frontend skeleton**: Sidebar accordion + `App.jsx` route + empty `MarketDashboardPage.jsx`. Verify: click sidebar, see the page render with section name.
3. **Phase C — Snapshot grid**: port chart libs + `SnapshotGrid.jsx` + wire `snapshot()` API. Verify: see snapshot cards with prices + change pct.
4. **Phase D — Price charts**: `PriceChart.jsx` + wire `history()` per product. Verify: see line charts for each product.
5. **Phase E — Regional + News**: add backend `/regional/{id}` (simplified, price-by-region) + `/news` (returns empty + note in v1) + create `RegionalTable.jsx` + `NewsPanel.jsx` (info banner in v1, real news requires EDIA's AI event pipeline — out of scope). Verify: full v1 dashboard for all 4 sections.

## 8. Risks & Mitigations

- **EDIA MySQL unreachable** → 503 + `{"error": "edia_unavailable"}` from backend; frontend shows "EDIA data source offline" banner. Rest of zhanlu keeps working — no shared state.
- **EDIA schema drift** → `Table(autoload_with=engine)` auto-adapts; renamed columns surface as missing attributes at first query → visible in logs.
- **Performance** → `pool_pre_ping=True` + `connect_timeout=3` means a dead host fails in 3s, not 30s. ETag headers on responses for 60s client-side cache.
- **Sidebar noise in dev** → accordion only renders when `/market-dashboard/enabled` returns true (i.e. `EDIA_MYSQL_URL` is set). No dead nav item.

## 9. Open Questions Resolved

All 3 open questions from the brainstorming session are resolved by section 2 above. No outstanding questions.

## 10. Out of Scope

- Write operations to EDIA's MySQL DB (read-only mirror).
- Real-time streaming / websocket push (uses standard HTTP polling / cache invalidation).
- New chart types beyond what EDIA already ships.
- Authentication changes (uses existing zhanlu session auth).
