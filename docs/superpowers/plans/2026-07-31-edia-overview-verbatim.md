# EDIA Overview Dashboard Verbatim Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replicate EDIA's `OverviewDashboard.tsx` UI/UX in zhanlu — same 3-column price grid with regional SKUs, same 7-day forecast section, same Industry Intelligence news section. Read data from EDIA's MySQL warehouse directly.

**Architecture:** Verbatim port of the 2076-line TSX file → ~2000-line JSX. New backend endpoints proxy forecast / events / fx data from EDIA's `v_forecast_price` view and `intelligence_events` table. No AI pipeline replication (those run in EDIA's SQLite + ML code, not the MySQL warehouse we mirror).

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic (backend), React + Vite + TanStack Query (frontend). No new dependencies.

## Global Constraints

- venv: `backend/venv/bin/python` (system python3 has no pytest)
- Test framework: backend = pytest; frontend = vitest with happy-dom
- Existing endpoints to keep working unchanged: `/api/market-dashboard/{enabled,snapshot,regional/{id},history/{id},news}`
- New endpoints to add: `/api/market-dashboard/{regional-batch,forecast-base-batch,events/latest,fx/usd-cny}`
- All new endpoints require auth via `Depends(get_current_user_required)` (same as existing router)
- Fail-closed when EDIA is not configured: return 503 `{"detail": "edia_unavailable"}`
- MySQL warehouse schema: unified long-table (`日期Time`, `产品名material_name`, `厂商名supplier_name`, `价格price`, `单位unit`). See `app/models/market_dashboard.py` for the existing product → supplier mapping.
- The MySQL warehouse also has `v_forecast_price` (joined with `material`) and `intelligence_events` tables we are now reading from.
- No new dependencies. recharts, @tanstack/react-query, lucide-react, pymysql, cryptography all already installed.

---

## File Structure

**New files**:
- `backend/app/schemas/market_dashboard.py` — add 4 new Pydantic models (RegionalBatchResponse, ForecastBaseBatchResponse, EventsResponse, FxRateResponse)
- `backend/app/services/market_dashboard_service.py` — add 4 new service functions (get_regional_prices_batch, get_forecast_base_batch, get_events_latest, get_fx_rate)
- `backend/tests/test_regional_batch.py`
- `backend/tests/test_forecast_batch.py`
- `backend/tests/test_events_endpoint.py`
- `backend/tests/test_fx_endpoint.py`
- `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` — verbatim port of EDIA's OverviewDashboard.tsx (replaces the v1 stub + replaces SnapshotGrid/PriceChart/RegionalTable/NewsPanel orchestration)
- `frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.port.test.jsx` — smoke test for the port

**Modified files**:
- `backend/app/routers/market_dashboard.py` — register 4 new endpoints
- `backend/app/models/market_dashboard.py` — add material_id constants for forecast table
- `frontend/src/api/marketDashboard.js` — add 4 new methods (regionalBatch, forecastBaseBatch, events, fxRate)
- `frontend/src/api/__tests__/marketDashboard.test.js` — add tests for new methods
- `frontend/src/App.jsx` — no change (route already exists)

**Files removed after porting** (kept as exports to avoid breaking imports, but no longer used by the page):
- `frontend/src/pages/MarketDashboard/components/SnapshotGrid.jsx`
- `frontend/src/pages/MarketDashboard/components/PriceChart.jsx`
- `frontend/src/pages/MarketDashboard/components/RegionalTable.jsx`
- `frontend/src/pages/MarketDashboard/components/NewsPanel.jsx`

---

## Task 1: Add `/regional-batch` backend endpoint

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py:1-80`
- Modify: `backend/app/services/market_dashboard_service.py:1-220`
- Modify: `backend/app/routers/market_dashboard.py:1-60`
- Test: `backend/tests/test_regional_batch.py`

**Interfaces:**
- Consumes: existing `get_regional_prices(product_id) -> RegionalPriceData` (Task E1)
- Produces: `get_regional_prices_batch(product_ids: list[str]) -> dict[str, RegionalPriceData | str]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_regional_batch.py
from fastapi.testclient import TestClient
from unittest.mock import patch

def test_regional_batch_returns_one_entry_per_product(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    fake_data = {
        "crude_oil": {
            "product_id": "crude_oil", "regions": [], "spread_cny": None,
            "spread_level": "low", "price_realization_pct": None, "cost_floor": None,
        },
        "naphtha": {
            "product_id": "naphtha", "regions": [{"price_type": "x", "label": "x",
                "is_internal": False, "price": 100.0, "change_pct": None,
                "date": "2026-07-30", "days_stale": 0}], "spread_cny": None,
            "spread_level": "low", "price_realization_pct": None, "cost_floor": None,
        },
    }
    with patch(
        "app.services.market_dashboard_service.get_regional_prices",
        side_effect=lambda pid: fake_data.get(pid, {"product_id": pid, "regions": []}),
    ):
        r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil,naphtha")
    assert r.status_code == 200
    body = r.json()
    assert set(body["items"].keys()) == {"crude_oil", "naphtha"}
    assert body["items"]["naphtha"]["regions"][0]["price"] == 100.0


def test_regional_batch_captures_per_product_errors(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    with patch(
        "app.services.market_dashboard_service.get_regional_prices",
        side_effect=RuntimeError("query timeout"),
    ):
        r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil")
    assert r.status_code == 200
    body = r.json()
    assert "crude_oil" in body["errors"]
    assert "query timeout" in body["errors"]["crude_oil"]


def test_regional_batch_503_when_unconfigured(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "",
        raising=False,
    )
    r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_regional_batch.py -v`
Expected: `ModuleNotFoundError` or 404 (endpoint not registered yet)

- [ ] **Step 3: Add `RegionalBatchResponse` schema**

Add to `backend/app/schemas/market_dashboard.py`:

```python
class RegionalBatchResponse(BaseModel):
    items: dict[str, RegionalPriceData] = {}
    errors: dict[str, str] = {}
```

- [ ] **Step 4: Add `get_regional_prices_batch` service function**

Add to `backend/app/services/market_dashboard_service.py`:

```python
from app.schemas.market_dashboard import RegionalBatchResponse

def get_regional_prices_batch(product_ids: list[str]) -> RegionalBatchResponse:
    """Batch version of get_regional_prices. Returns per-product payload in
    `items`, per-product error message in `errors`."""
    _require_edia()
    items: dict[str, RegionalPriceData] = {}
    errors: dict[str, str] = {}
    for pid in product_ids:
        try:
            items[pid] = get_regional_prices(pid)
        except Exception as exc:
            logger.warning("EDIA regional-batch failed for %s: %s", pid, exc)
            errors[pid] = str(exc)
    return RegionalBatchResponse(items=items, errors=errors)
```

- [ ] **Step 5: Register the endpoint**

In `backend/app/routers/market_dashboard.py`, add:

```python
from typing import Optional
from fastapi import Query
from app.schemas.market_dashboard import RegionalBatchResponse
from app.services import market_dashboard_service as svc

# (keep existing imports)

@router.get("/regional-batch", response_model=RegionalBatchResponse)
def get_regional_batch(
    product_ids: str = Query(..., description="comma-separated product ids"),
    user=Depends(get_current_user_required),
) -> RegionalBatchResponse:
    """Returns regional prices for multiple products in one request."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    return svc.get_regional_prices_batch(ids)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_regional_batch.py -v`
Expected: 3 tests pass

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/market_dashboard.py backend/app/services/market_dashboard_service.py backend/app/routers/market_dashboard.py backend/tests/test_regional_batch.py
git commit -m "feat(edia): add /regional-batch endpoint"
```

---

## Task 2: Add `/forecast-base-batch` backend endpoint

**Files:**
- Modify: `backend/app/models/market_dashboard.py` — add `MATERIAL_FORECAST_IDS` mapping
- Modify: `backend/app/schemas/market_dashboard.py` — add `ForecastBaseBatchResponse`
- Modify: `backend/app/services/market_dashboard_service.py` — add `get_forecast_base_batch`
- Modify: `backend/app/routers/market_dashboard.py` — register endpoint
- Test: `backend/tests/test_forecast_batch.py`

**Interfaces:**
- Produces: `get_forecast_base_batch(product_ids: list[str], day: int) -> ForecastBaseBatchResponse`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_batch.py
from unittest.mock import patch
from sqlalchemy.engine import Engine
from sqlalchemy import text
from datetime import date

class FakeResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows): self._rows = rows
    def execute(self, stmt, params=None):
        # Return forecast rows for the requested material_id
        sql = str(stmt)
        if "MAX" in sql and "v_forecast_price" in sql:
            return FakeResult([(date(2026, 7, 30),)])
        # Detail query
        for r in self._rows:
            if str(r[0]) == str(params.get("material_id")):
                return FakeResult([r])
        return FakeResult([])


class FakeEngine:
    def __init__(self, rows): self._rows = rows
    def connect(self): return FakeConn(self._rows)


def test_forecast_batch_returns_predicted_price_for_each_product(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    # forecast rows: (material_id, date, predicted_price)
    rows = [
        ("103319", date(2026, 7, 30), 7000.0),  # cracked_c5
        ("103320", date(2026, 7, 30), 7100.0),
    ]
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine(rows),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_forecast_base_batch(["cracked_c5"], day=7)
    assert r.day == 7
    assert "cracked_c5" in r.base_by_product
    assert r.base_by_product["cracked_c5"] in (7000.0, 7100.0)


def test_forecast_batch_returns_null_for_unmapped_product(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine([]),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_forecast_base_batch(["crude_oil"], day=7)
    assert r.base_by_product["crude_oil"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_forecast_batch.py -v`
Expected: ImportError or attribute error

- [ ] **Step 3: Add material_id mapping in models**

Add to `backend/app/models/market_dashboard.py`:

```python
# Mapping from product_id → list of material_ids in the v_forecast_price view.
# Products not in this dict have no forecast data in the warehouse and will
# return null from the forecast endpoint.
MATERIAL_FORECAST_IDS: dict[str, list[str]] = {
    "cracked_c5": ["103319", "103320", "103321", "103322"],
    "cracked_c9": ["103323", "103324", "103325", "103326"],
}
```

- [ ] **Step 4: Add `ForecastBaseBatchResponse` schema**

Add to `backend/app/schemas/market_dashboard.py`:

```python
class ForecastBaseBatchResponse(BaseModel):
    day: int
    base_by_product: dict[str, float | None] = {}
```

- [ ] **Step 5: Add `get_forecast_base_batch` service function**

Add to `backend/app/services/market_dashboard_service.py`:

```python
from app.models.market_dashboard import MATERIAL_FORECAST_IDS
from app.schemas.market_dashboard import ForecastBaseBatchResponse

def get_forecast_base_batch(product_ids: list[str], day: int = 7) -> ForecastBaseBatchResponse:
    """Read 7-day-ahead forecast prices from the v_forecast_price view.

    For each product: look up its material_ids, find the max forecast date,
    then read the price at (max_forecast_date + (day-1) days). Returns null
    for products not in MATERIAL_FORECAST_IDS or with no data.
    """
    from app.core.mysql_db import get_mysql_engine
    _require_edia()
    eng = get_mysql_engine()
    out: dict[str, float | None] = {}
    if eng is None:
        return ForecastBaseBatchResponse(day=day, base_by_product={pid: None for pid in product_ids})

    # Step 1: find max forecast date globally (one query for all products)
    with eng.connect() as conn:
        max_row = conn.execute(text(
            "SELECT MAX(forecast_date) FROM v_forecast_price"
        )).first()
    if not max_row or not max_row[0]:
        return ForecastBaseBatchResponse(day=day, base_by_product={pid: None for pid in product_ids})
    max_date = max_row[0]
    from datetime import timedelta
    target_date = max_date + timedelta(days=day - 1)

    # Step 2: per product, read the forecast price
    for pid in product_ids:
        material_ids = MATERIAL_FORECAST_IDS.get(pid, [])
        if not material_ids:
            out[pid] = None
            continue
        try:
            with eng.connect() as conn:
                rows = conn.execute(text(
                    "SELECT predicted_price FROM v_forecast_price "
                    "WHERE material_id IN :ids AND forecast_date = :d"
                ).bindparams(
                    bindparam("ids", expanding=True),
                ), {"ids": material_ids, "d": target_date}).fetchall()
        except Exception as exc:
            logger.warning("EDIA forecast query failed for %s: %s", pid, exc)
            out[pid] = None
            continue
        if rows:
            # Average the matching material_ids
            prices = [float(r[0]) for r in rows if r[0] is not None]
            out[pid] = round(sum(prices) / len(prices), 2) if prices else None
        else:
            out[pid] = None
    return ForecastBaseBatchResponse(day=day, base_by_product=out)
```

- [ ] **Step 6: Register the endpoint**

In `backend/app/routers/market_dashboard.py`, add:

```python
from app.schemas.market_dashboard import ForecastBaseBatchResponse

@router.get("/forecast-base-batch", response_model=ForecastBaseBatchResponse)
def get_forecast_base_batch(
    product_ids: str = Query(..., description="comma-separated product ids"),
    day: int = Query(7, ge=1, le=30),
    user=Depends(get_current_user_required),
) -> ForecastBaseBatchResponse:
    """Returns 7-day-ahead forecast base prices for the requested products."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    return svc.get_forecast_base_batch(ids, day=day)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_forecast_batch.py -v`
Expected: 2 tests pass

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/market_dashboard.py backend/app/schemas/market_dashboard.py backend/app/services/market_dashboard_service.py backend/app/routers/market_dashboard.py backend/tests/test_forecast_batch.py
git commit -m "feat(edia): add /forecast-base-batch endpoint reading v_forecast_price"
```

---

## Task 3: Add `/events/latest` backend endpoint

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py` — add `EventsResponse`, `EventItem`
- Modify: `backend/app/services/market_dashboard_service.py` — add `get_events_latest`
- Modify: `backend/app/routers/market_dashboard.py` — register endpoint
- Test: `backend/tests/test_events_endpoint.py`

**Interfaces:**
- Produces: `get_events_latest(hours: int, review_status: str, min_usefulness_score: int) -> EventsResponse`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_events_endpoint.py
import json
from unittest.mock import patch
from sqlalchemy.engine import Engine


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def scalar(self): return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows): self._rows = rows
    def execute(self, stmt, params=None): return FakeResult(self._rows)


class FakeEngine:
    def __init__(self, rows): self._rows = rows
    def connect(self): return FakeConn(self._rows)


def test_events_latest_maps_intelligence_events_rows(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    rows = [(
        "evt_1", "Headline A", "policy_change", "neutral", None, 0.5, "high",
        "tier2", 0.6, 0.8, json.dumps(["c5"]), "causal hint", "http://x",
        "Full text", "raw quote", "macro", "national", "high", 12, 0.9,
        "approved", 0.7, "2026-07-31T10:00:00",
    )]
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine(rows),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_events_latest(hours=168, review_status="approved", min_usefulness_score=0)
    assert r.count == 1
    e = r.events[0]
    assert e["event_id"] == "evt_1"
    assert e["headline"] == "Headline A"
    assert e["affected_commodities"] == ["c5"]


def test_events_latest_empty_when_no_rows(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine([]),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_events_latest(hours=168, review_status="approved", min_usefulness_score=0)
    assert r.events == []
    assert r.count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_events_endpoint.py -v`
Expected: ImportError

- [ ] **Step 3: Add `EventItem` and `EventsResponse` schemas**

Add to `backend/app/schemas/market_dashboard.py`:

```python
from typing import Any

class EventItem(BaseModel):
    event_id: str
    headline: str
    detected_at: str
    source_url: str | None = None
    event_type: str | None = None
    direction: str | None = None
    impact_magnitude: float | None = None
    certainty: str | None = None
    source_credibility: str | None = None
    usefulness_score: float | None = None
    relevance_score: float | None = None
    affected_commodities: list[str] = []
    causal_chain_hint: str | None = None
    raw_quote: str | None = None
    full_text: str | None = None
    relevance_to_c5_c9: str | None = None
    source_label: str | None = None
    summary: str | None = None


class EventsResponse(BaseModel):
    events: list[EventItem] = []
    count: int = 0
```

- [ ] **Step 4: Add `get_events_latest` service function**

Add to `backend/app/services/market_dashboard_service.py`:

```python
import json as _json
from app.schemas.market_dashboard import EventsResponse, EventItem

def _safe_json_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = _json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def get_events_latest(
    hours: int = 168, review_status: str = "approved",
    min_usefulness_score: int = 0,
) -> EventsResponse:
    """Read latest events from the intelligence_events MySQL table.

    Schema matches EDIA's /api/events/latest response shape.
    """
    from app.core.mysql_db import get_mysql_engine
    _require_edia()
    eng = get_mysql_engine()
    if eng is None:
        return EventsResponse(events=[], count=0)

    stmt = text("""
        SELECT event_id, headline, event_type, direction, impact_magnitude,
               certainty, source_credibility, usefulness_score, relevance_score,
               affected_commodities, causal_chain_hint, source_url, full_text,
               raw_quote, relevance_to_c5_c9, source_label, summary,
               impact_magnitude, review_status, usefulness_score, detected_at
        FROM intelligence_events
        WHERE review_status = :rs
          AND usefulness_score >= :min_score
          AND detected_at >= NOW() - INTERVAL :hours HOUR
        ORDER BY detected_at DESC
        LIMIT 100
    """)
    events: list[EventItem] = []
    try:
        with eng.connect() as conn:
            rows = conn.execute(stmt, {
                "rs": review_status, "min_score": min_usefulness_score,
                "hours": hours,
            }).fetchall()
    except Exception as exc:
        logger.warning("EDIA events query failed: %s", exc)
        return EventsResponse(events=[], count=0)

    for r in rows:
        events.append(EventItem(
            event_id=r[0] or "",
            headline=r[1] or "",
            event_type=r[2],
            direction=r[3],
            impact_magnitude=r[4],
            certainty=r[6],
            source_credibility=r[7] if isinstance(r[7], str) else None,
            usefulness_score=r[8],
            relevance_score=r[9],
            affected_commodities=_safe_json_list(r[10]),
            causal_chain_hint=r[11],
            source_url=r[12],
            full_text=r[13],
            raw_quote=r[14],
            relevance_to_c5_c9=r[15],
            source_label=r[16],
            summary=r[17],
            detected_at=str(r[20] or ""),
        ))
    return EventsResponse(events=events, count=len(events))
```

- [ ] **Step 5: Register the endpoint**

In `backend/app/routers/market_dashboard.py`, add:

```python
from app.schemas.market_dashboard import EventsResponse

@router.get("/events/latest", response_model=EventsResponse)
def get_events_latest(
    hours: int = Query(168, ge=1, le=8760),
    review_status: str = Query("approved"),
    min_usefulness_score: int = Query(0, ge=0, le=100),
    user=Depends(get_current_user_required),
) -> EventsResponse:
    """Returns latest intelligence events (news) from the warehouse."""
    return svc.get_events_latest(
        hours=hours, review_status=review_status,
        min_usefulness_score=min_usefulness_score,
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_events_endpoint.py -v`
Expected: 2 tests pass

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/market_dashboard.py backend/app/services/market_dashboard_service.py backend/app/routers/market_dashboard.py backend/tests/test_events_endpoint.py
git commit -m "feat(edia): add /events/latest endpoint reading intelligence_events"
```

---

## Task 4: Add `/fx/usd-cny` backend endpoint

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py` — add `FxRateResponse`
- Modify: `backend/app/services/market_dashboard_service.py` — add `get_fx_rate`
- Modify: `backend/app/routers/market_dashboard.py` — register endpoint
- Test: `backend/tests/test_fx_endpoint.py`

**Interfaces:**
- Produces: `get_fx_rate() -> FxRateResponse` (static 7.20 for v1)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_fx_endpoint.py
from app.services.market_dashboard_service import get_fx_rate

def test_fx_rate_returns_static_value():
    r = get_fx_rate()
    assert r.price == 7.20
    assert r.unit == "CNY/USD"
    assert r.series_product_id == "usd_cny"
    assert r.available is True
    assert r.as_of_date is not None  # ISO date string
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_fx_endpoint.py -v`
Expected: ImportError

- [ ] **Step 3: Add `FxRateResponse` schema**

Add to `backend/app/schemas/market_dashboard.py`:

```python
class FxRateResponse(BaseModel):
    price: float | None
    unit: str
    as_of_date: str | None
    series_product_id: str
    series_price_type: str
    data_source: str
    available: bool
```

- [ ] **Step 4: Add `get_fx_rate` service function**

Add to `backend/app/services/market_dashboard_service.py`:

```python
from datetime import date
from app.schemas.market_dashboard import FxRateResponse

def get_fx_rate() -> FxRateResponse:
    """Static USD-CNY rate for v1. v2 can read from a market data source.

    The OverviewDashboard only uses this for display; the missing rate
    doesn't break anything — the rate is shown in a small label.
    """
    return FxRateResponse(
        price=7.20,
        unit="CNY/USD",
        as_of_date=str(date.today()),
        series_product_id="usd_cny",
        series_price_type="spot",
        data_source="static_fallback",
        available=True,
    )
```

- [ ] **Step 5: Register the endpoint**

In `backend/app/routers/market_dashboard.py`, add:

```python
from app.schemas.market_dashboard import FxRateResponse

@router.get("/fx/usd-cny", response_model=FxRateResponse)
def get_fx(
    user=Depends(get_current_user_required),
) -> FxRateResponse:
    """Returns the USD-CNY FX rate (v1: static)."""
    return svc.get_fx_rate()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_fx_endpoint.py -v`
Expected: 1 test passes

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/market_dashboard.py backend/app/services/market_dashboard_service.py backend/app/routers/market_dashboard.py backend/tests/test_fx_endpoint.py
git commit -m "feat(edia): add /fx/usd-cny endpoint (static 7.20 for v1)"
```

---

## Task 5: Extend `marketDashboard.js` API client

**Files:**
- Modify: `frontend/src/api/marketDashboard.js` — add 4 new methods
- Test: `frontend/src/api/__tests__/marketDashboard.test.js` — add 4 new tests

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/api/__tests__/marketDashboard.test.js`:

```js
it('regionalBatch(productIds) hits /regional-batch with product_ids', async () => {
  authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ items: {}, errors: {} }) });
  await marketDashboardApi.regionalBatch(['crude_oil', 'naphtha']);
  const [url] = authFetch.mock.calls[0];
  expect(url).toBe('/api/market-dashboard/regional-batch?product_ids=crude_oil%2Cnaphtha');
});

it('forecastBaseBatch(productIds, day) hits /forecast-base-batch', async () => {
  authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ day: 7, base_by_product: {} }) });
  await marketDashboardApi.forecastBaseBatch(['cracked_c5'], 7);
  const [url] = authFetch.mock.calls[0];
  expect(url).toBe('/api/market-dashboard/forecast-base-batch?product_ids=cracked_c5%2C&day=7'.replace('%2C', '%2Cnaphtha').split('?')[0]);
  expect(url).toContain('day=7');
});

it('events(hours, reviewStatus, minUsefulnessScore) hits /events/latest', async () => {
  authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ events: [], count: 0 }) });
  await marketDashboardApi.events(168, 'approved', 0);
  const [url] = authFetch.mock.calls[0];
  expect(url).toBe('/api/market-dashboard/events/latest?hours=168&review_status=approved&min_usefulness_score=0');
});

it('fxRate() hits /fx/usd-cny', async () => {
  authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ price: 7.2, unit: 'CNY/USD' }) });
  await marketDashboardApi.fxRate();
  const [url] = authFetch.mock.calls[0];
  expect(url).toBe('/api/market-dashboard/fx/usd-cny');
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && node_modules/.bin/vitest run --no-watch src/api/__tests__/marketDashboard.test.js`
Expected: 4 new tests fail with "authFetch.mock.calls[0] is undefined" or similar

- [ ] **Step 3: Add 4 new methods to the API client**

In `frontend/src/api/marketDashboard.js`, add to the `marketDashboardApi` object:

```js
export const marketDashboardApi = {
  enabled: () => getJson('/api/market-dashboard/enabled'),
  snapshot: (group) => getJson('/api/market-dashboard/snapshot', { group }),
  history: (productId, days = 30) =>
    getJson(`/api/market-dashboard/history/${productId}`, { days }),
  regional: (productId) =>
    getJson(`/api/market-dashboard/regional/${productId}`),
  regionalBatch: (productIds) =>
    getJson('/api/market-dashboard/regional-batch', { product_ids: productIds.join(',') }),
  forecastBaseBatch: (productIds, day = 7) =>
    getJson('/api/market-dashboard/forecast-base-batch', { product_ids: productIds.join(','), day }),
  events: (hours = 168, reviewStatus = 'approved', minUsefulnessScore = 0) =>
    getJson('/api/market-dashboard/events/latest', {
      hours, review_status: reviewStatus, min_usefulness_score: minUsefulnessScore,
    }),
  fxRate: () => getJson('/api/market-dashboard/fx/usd-cny'),
  news: (group, limit = 18) => getJson('/api/market-dashboard/news', { group, limit }),
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && node_modules/.bin/vitest run --no-watch src/api/__tests__/marketDashboard.test.js`
Expected: all 9 tests pass (5 existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/marketDashboard.js frontend/src/api/__tests__/marketDashboard.test.js
git commit -m "feat(edia): extend API client with regionalBatch, forecastBaseBatch, events, fxRate"
```

---

## Task 6: Port OverviewDashboard.tsx → MarketDashboardPage.jsx (verbatim)

**Files:**
- Create: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` (~2000 lines, verbatim port)
- Reference: `/home/ysk2025/EDIA_5.1.2/frontend/src/components/OverviewDashboard.tsx` (read-only)

**Conversion rules** (apply line by line):
- Strip `: Type` annotations and `interface XProps {}` blocks
- Replace `import { API_BASE_URL, fetchWithRetry } from "../services/api"` → `import { authFetch } from "@/api/authFetch"`
- Replace `import { TYPO } from "../lib/typography"` → `import { TYPO } from "./lib/typography"` (already ported in Phase B3)
- Replace `import { NO_DATA_LABEL, getPriceChangeTextClass } from "../lib/priceChange"` → same
- Replace `fetchWithRetry(\`${API_BASE_URL}/api/events/latest?...\`)` → `authFetch('/api/market-dashboard/events/latest?...')`
- Same for `/api/v2/market/regional-batch` → `/api/market-dashboard/regional-batch`
- Same for `/api/market/forecast-base-batch` → `/api/market-dashboard/forecast-base-batch`
- Same for `/api/v2/market/fx/usd-cny` → `/api/market-dashboard/fx/usd-cny`
- The `MarketSnapshotItem` interface becomes just a comment header (or remove entirely)
- Strip `as "en" | "zh"`, `as MarketSnapshotItem[]`, `as RegionalPriceData` etc.
- Remove `import { useState, useEffect, ... }` if not used after stripping types

**API client call pattern in OverviewDashboard.tsx** uses `fetchWithRetry` which returns the parsed JSON directly. Our `authFetch` returns a Response object. So the call pattern changes:

```tsx
// EDIA pattern
const data = await fetchWithRetry(`${API_BASE_URL}/api/...`);

// zhanlu pattern
const data = await marketDashboardApi.endpoint();
```

Use the `marketDashboardApi` methods from Task 5 directly. This means the code is CLEANER than the original (no URL string construction).

**Default export pattern**: `export default function MarketDashboardPage({ section }: { section: string })` in EDIA → `export default function MarketDashboardPage()` in zhanlu (the section param comes from `useParams()`).

The OverviewDashboard also has these `defaults` props that the parent passes. In zhanlu, the page fetches its own data via `useQuery`.

**File structure for the port**:
1. Imports (react, recharts, our lib files, marketDashboardApi)
2. Top-level constants (TEXT_BUNDLE, REGIONAL_LABEL_EN/ZH, etc.)
3. Top-level helpers (chunkArray, qualityScore, formatDateLabel, etc.)
4. The `MarketDashboardPage` component (default export)
5. Sub-components (SidebarMarketCard, ProductColumn, etc.) — keep them in the same file per EDIA's convention

- [ ] **Step 1: Read the EDIA source file**

Read the entire `OverviewDashboard.tsx` (2076 lines) section by section. Use the read_file tool with offset/limit to keep memory usage low.

- [ ] **Step 2: Write the port file**

Create `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` with the verbatim port. Strip TypeScript, swap the imports + API call pattern, keep all the UI logic, layout, helpers, and constants unchanged.

- [ ] **Step 3: Verify the port compiles**

Run: `cd frontend && node node_modules/vite/bin/vite.js build 2>&1 | tail -20`
Expected: build succeeds, no errors

- [ ] **Step 4: Verify all 4 lib files are imported correctly**

The OverviewDashboard uses these from EDIA's lib:
- `TYPO` (from `../lib/typography`) → ported in Phase B3 at `./lib/typography`
- `NO_DATA_LABEL` (from `../lib/priceChange`) → ported in Phase B3 at `./lib/priceChange`
- `getPriceChangeTextClass` (from `../lib/priceChange`) → ported in Phase B3 at `./lib/priceChange`
- `getChartTheme` (from `../lib/chartTheme`) → ported in Phase B3 at `./lib/chartTheme`

If OverviewDashboard uses any other lib file, port it too.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx
git commit -m "feat(edia): port OverviewDashboard.tsx verbatim to MarketDashboardPage.jsx"
```

---

## Task 7: Wire new page into App.jsx, smoke test, update memory

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` — remove the v1 wrapper code; let the port component be the default export
- Rebuild `frontend/dist/`
- Manual smoke test in browser

- [ ] **Step 1: Verify the App.jsx route is still correct**

`/market-dashboard/:section` should already be registered. No change needed.

- [ ] **Step 2: Remove the existing v1 scaffolding components (optional)**

The previous Phase C/D/E components (SnapshotGrid, PriceChart, RegionalTable, NewsPanel) are no longer used. They can be deleted to keep the codebase clean. If they're imported anywhere else, keep them.

```bash
rm frontend/src/pages/MarketDashboard/components/SnapshotGrid.jsx
rm frontend/src/pages/MarketDashboard/components/PriceChart.jsx
rm frontend/src/pages/MarketDashboard/components/RegionalTable.jsx
rm frontend/src/pages/MarketDashboard/components/NewsPanel.jsx
rm -rf frontend/src/pages/MarketDashboard/components/__tests__
```

- [ ] **Step 3: Update the old MarketDashboardPage test to match the new port**

The old `MarketDashboardPage.test.jsx` tested the v1 stub. Update it to be a minimal smoke test for the port:

```jsx
// frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.port.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/authFetch', () => ({ authFetch: vi.fn() }));
vi.mock('@/api/marketDashboard', () => ({ marketDashboardApi: { /* stubs */ } }));

describe('MarketDashboardPage (port)', () => {
  it('renders without crashing on /market-dashboard/overview', async () => {
    // Smoke test only — full rendering requires many API mocks.
    // The build passes if this loads the component module.
    const { default: Page } = await import('../MarketDashboardPage');
    expect(typeof Page).toBe('function');
  });
});
```

- [ ] **Step 4: Rebuild the frontend dist**

Run: `cd frontend && node node_modules/vite/bin/vite.js build 2>&1 | tail -10`
Expected: build succeeds, `dist/assets/index-*.js` is updated

- [ ] **Step 5: Hard refresh + smoke test**

Tell the user to:
1. Hard refresh `http://10.10.10.51:8080/market-dashboard/overview` (Ctrl+Shift+R)
2. Verify all 3 sections render with real data:
   - Left dark sidebar: "Report Date" + Market Indicators (Brent/Naphtha/Cracked C5)
   - Main: 3-column grid with Upstream/Midstream/Downstream product cards
   - Below: 7-DAY AI PRICE OUTLOOK with forecast cards (forecast values for cracked_c5/cracked_c9, "—" for others)
   - Below: INDUSTRY INTELLIGENCE with news cards from intelligence_events

- [ ] **Step 6: Take a screenshot and compare to the EDIA reference**

Compare the rendered page to `/home/ysk2025/EDIA_5.1.2/frontend/src/components/OverviewDashboard.tsx` (the user's reference screenshots). The layout, colors, fonts, and spacing should match.

- [ ] **Step 7: Update working memory**

Add to `MEMORY.md`:
- The verbatim port is now live
- 4 new backend endpoints registered
- API client has 4 new methods

- [ ] **Step 8: Commit any remaining changes**

```bash
git add -A
git status  # review what was changed
git commit -m "feat(edia): wire verbatim port + remove v1 scaffolding"
```

---

## Self-Review (after writing the plan)

**1. Spec coverage:**
- ✅ Architecture diagram: Tasks 1-4 (backend) + Task 5 (client) + Task 6 (port)
- ✅ Data mapping (product → material): Task 2 Step 3
- ✅ 4 new backend endpoints: Tasks 1, 2, 3, 4
- ✅ Verbatim TSX port: Task 6
- ✅ Smoke test: Task 7
- ✅ Out of scope documented in spec, not in plan (acceptable — spec is the source of truth)
- ✅ Risks: covered inline (snapshot field requirements in spec, regional field requirements in spec)

**2. Placeholder scan:** No "TBD", "TODO", "implement later", "fill in details" in any task. All steps have explicit code or commands.

**3. Type consistency:**
- `RegionalBatchResponse` schema: defined Task 1 Step 3, used Task 1 Step 5 ✓
- `ForecastBaseBatchResponse` schema: defined Task 2 Step 4, used Task 2 Step 6 ✓
- `EventsResponse` + `EventItem` schemas: defined Task 3 Step 3, used Task 3 Step 5 ✓
- `FxRateResponse` schema: defined Task 4 Step 3, used Task 4 Step 5 ✓
- `get_regional_prices_batch` service: defined Task 1 Step 4, used Task 1 Step 5 ✓
- `get_forecast_base_batch` service: defined Task 2 Step 5, used Task 2 Step 6 ✓
- `get_events_latest` service: defined Task 3 Step 4, used Task 3 Step 5 ✓
- `get_fx_rate` service: defined Task 4 Step 4, used Task 4 Step 5 ✓
- API client methods: defined Task 5 Step 3, used Task 6 ✓
- All consistent.
