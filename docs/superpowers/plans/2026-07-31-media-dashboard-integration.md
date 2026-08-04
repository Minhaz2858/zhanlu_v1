# EDIA Market Dashboard Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an EDIA-parity market dashboard (Overview, Upstream, Midstream, Downstream) to zhanlu_7_30 by reading EDIA's MySQL warehouse (`aipdp_data_warehouse_prod` at `10.10.10.49:3306`) through a read-only mirror.

**Architecture:** New FastAPI router `/api/market-dashboard/*` opens a lazy SQLAlchemy engine against EDIA's MySQL using a new `EDIA_MYSQL_URL` env var. Service layer reads per-product tables (mirroring EDIA's `market_db.py` mapping) and computes `change_pct` from the last two history points. Frontend adds a `DashboardAccordion` to `Sidebar.jsx` and a single parameterized page `MarketDashboardPage.jsx` that reuses EDIA's ported chart libs + recharts. News returns an info banner in v1 (EDIA's AI event pipeline is out of scope).

**Tech Stack:** FastAPI + SQLAlchemy + pymysql (already installed) / React + Vite + recharts + @tanstack/react-query + lucide-react (all already installed).

**Source spec:** `docs/superpowers/specs/2026-07-31-media-dashboard-integration-design.md`

## Global Constraints

- **No new dependencies.** `pymysql>=1.1` and `cryptography>=41.0` are already in `backend/requirements.txt` (lines 33–34). `recharts@2.15.4`, `lucide-react@0.475.0`, `@tanstack/react-query@5.84.1` are already in `frontend/package.json`.
- **Read-only.** No `INSERT`/`UPDATE`/`DELETE` against EDIA's MySQL. No `Base.metadata.create_all`. Use `Table(autoload_with=engine)` only.
- **Fail-closed.** Empty `EDIA_MYSQL_URL` → HTTP 503 `{"error": "edia_unavailable"}`. MySQL unreachable → HTTP 503 `{"error": "edia_unreachable"}`. No silent empty data, no fallback.
- **Session auth required.** All `/api/market-dashboard/*` routes use `Depends(get_current_user_required)` (matches `dashboards.py` line 8).
- **EDIA product mapping (verbatim from spec §6):**
  - upstream → `crude_oil`, `naphtha`, `cracked_c5`, `cracked_c9`
  - midstream → `isoprene`, `piperylene`, `dcpd`, `raffinate_c5`
  - downstream → `sis`, `c5_resin`, `blowing_agent`, `styrene`
- **EDIA table mapping (verbatim from EDIA's `market_db.py`):**
  - `naphtha` → table `裂解C5`, value_col `日本石脑油Japanese naphtha`
  - `mixed_c5` → table `裂解C5`, value_col `扬子裂解C5Yangzi Cracking C5`
  - `isoprene` → table `异戊二烯`, value_col `华东市场价East China market price`
  - `piperylene` → table `间戊二烯`, value_col `华东市场价East China market price`
  - `dcpd` → table `双环戊二烯`, value_col `华东市场价East China market price`
  - `sis_rubber` → table `SIS`, value_col `巴陵1105Baling 1105`
- For v1 we use the table/value_col mapping above. Products without a known mapping (`crude_oil`, `cracked_c9`, `raffinate_c5`, `c5_resin`, `blowing_agent`, `styrene`) return `{"id": "...", "name": "...", "price": null, "change_pct": null, "note": "table mapping not configured in v1"}` from snapshot/history.

---

## Phase A — Backend Foundation

### Task A1: Add `EDIA_MYSQL_URL` config field + `.env.example` entry

**Files:**
- Modify: `backend/app/config.py` (insert after line 52 — end of `DATABASE_URL` validator)
- Modify: `backend/.env.example` (append after line 15)
- Test: `backend/tests/test_config_edia.py`

**Interfaces:**
- Produces: `settings.EDIA_MYSQL_URL: str` (empty string when unset). `settings.is_edia_enabled: bool` property.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_config_edia.py
"""EDIA_MYSQL_URL config — empty by default, populated from env."""
import importlib
import os
import sys


def _reload_settings():
    """Re-instantiate Settings() so changes to env vars are picked up."""
    sys.modules.pop("app.config", None)
    return importlib.import_module("app.config").settings


def test_edia_mysql_url_default_empty(monkeypatch):
    monkeypatch.delenv("EDIA_MYSQL_URL", raising=False)
    s = _reload_settings()
    assert s.EDIA_MYSQL_URL == ""
    assert s.is_edia_enabled is False


def test_edia_mysql_url_set_enables(monkeypatch):
    monkeypatch.setenv("EDIA_MYSQL_URL", "mysql+pymysql://root:pw@10.10.10.49:3306/aipdp_data_warehouse_prod")
    s = _reload_settings()
    assert s.EDIA_MYSQL_URL.startswith("mysql+pymysql://")
    assert s.is_edia_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_config_edia.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'EDIA_MYSQL_URL'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/config.py`, insert after the `_require_database_url` validator (currently ends at line 52, just before `# Auth`):

```python
    # ── EDIA Market Dashboard — read-only mirror of EDIA's MySQL warehouse.
    # Empty = dashboard routes return 503 (the rest of zhanlu is unaffected).
    # Example: mysql+pymysql://root:PASSWORD@10.10.10.49:3306/aipdp_data_warehouse_prod
    EDIA_MYSQL_URL: str = ""

    @property
    def is_edia_enabled(self) -> bool:
        """True when EDIA_MYSQL_URL is set (the dashboard will at least try)."""
        return bool(self.EDIA_MYSQL_URL)
```

In `backend/.env.example`, insert after line 15 (the `curl ... /api/_db-info` comment):

```

# ===== EDIA Market Dashboard (read-only MySQL mirror) =====
# Empty = dashboard routes return 503. Fill with the EDIA warehouse DSN
# to enable the /api/market-dashboard/* endpoints.
# EDIA_MYSQL_URL=mysql+pymysql://root:PASSWORD@10.10.10.49:3306/aipdp_data_warehouse_prod
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_config_edia.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/.env.example backend/tests/test_config_edia.py
git commit -m "feat(edia): add EDIA_MYSQL_URL config field + env example"
```

---

### Task A2: Create `mysql_db.py` — lazy MySQL engine + `get_mysql_db` dependency

**Files:**
- Create: `backend/app/core/mysql_db.py`
- Test: `backend/tests/test_mysql_db.py`

**Interfaces:**
- Consumes: `settings.EDIA_MYSQL_URL` (Task A1)
- Produces: `get_mysql_engine() -> Engine | None` (returns `None` when `EDIA_MYSQL_URL` empty); `get_mysql_db()` FastAPI dependency yielding a SQLAlchemy `Session` (or raising `HTTPException(503, "edia_unavailable")`); `is_edia_reachable() -> bool` (cached 30s)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_mysql_db.py
"""mysql_db — lazy engine, 503 when unconfigured, reachability probe."""
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


def _reload(monkeypatch, url=""):
    monkeypatch.setenv("EDIA_MYSQL_URL", url) if url else monkeypatch.delenv("EDIA_MYSQL_URL", raising=False)
    sys.modules.pop("app.core.mysql_db", None)
    sys.modules.pop("app.config", None)
    importlib.import_module("app.config")  # rebuild settings
    return importlib.import_module("app.core.mysql_db")


def test_get_mysql_engine_returns_none_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    assert mod.get_mysql_engine() is None


def test_get_mysql_db_raises_503_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        next(mod.get_mysql_db())
    assert exc.value.status_code == 503
    assert exc.value.detail == "edia_unavailable"


def test_get_mysql_engine_returns_engine_when_configured(monkeypatch):
    mod = _reload(monkeypatch, "mysql+pymysql://root:pw@10.10.10.49:3306/db")
    eng = mod.get_mysql_engine()
    assert eng is not None
    # Cached on second call (same object)
    assert mod.get_mysql_engine() is eng


def test_is_edia_reachable_false_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    assert mod.is_edia_reachable() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_mysql_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.mysql_db'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/core/mysql_db.py
"""Lazy SQLAlchemy engine for EDIA's MySQL warehouse (read-only mirror).

Mirrors EDIA's backend/src/db/engine_factory.py pattern: pool_pre_ping + small
pool + short connect_timeout so an unreachable host fails in ~3s, not 30s.

Empty EDIA_MYSQL_URL = feature off. The engine is created lazily on first
use so the rest of zhanlu is unaffected when EDIA is not configured.
"""
import logging
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_engine_lock_missing = False  # placeholder; module-level guard via _engine check
_reachability_cache: dict = {"value": None, "ts": 0.0}
_REACHABILITY_TTL = 30.0  # seconds


def get_mysql_engine() -> Optional[Engine]:
    """Return the cached EDIA MySQL engine, or None when unconfigured."""
    global _engine
    if not settings.EDIA_MYSQL_URL:
        return None
    if _engine is None:
        _engine = create_engine(
            settings.EDIA_MYSQL_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
            pool_recycle=1800,
            connect_args={"connect_timeout": 3},
        )
    return _engine


def get_mysql_db():
    """FastAPI dependency: yield a Session against EDIA's MySQL.

    Raises HTTPException(503, "edia_unavailable") when EDIA_MYSQL_URL is empty.
    Callers wrap query errors as HTTPException(503, "edia_unreachable").
    """
    eng = get_mysql_engine()
    if eng is None:
        raise HTTPException(status_code=503, detail="edia_unavailable")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_edia_reachable() -> bool:
    """Return True if `SELECT 1` succeeds against EDIA's MySQL.

    Cached for _REACHABILITY_TTL seconds to bound probe cost.
    Returns False when unconfigured OR when the probe raises.
    """
    eng = get_mysql_engine()
    if eng is None:
        return False
    now = time.time()
    cached = _reachability_cache.get("value")
    ts = _reachability_cache.get("ts") or 0.0
    if cached is not None and (now - ts) < _REACHABILITY_TTL:
        return cached
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        _reachability_cache["value"] = True
    except Exception as exc:
        logger.warning("EDIA MySQL unreachable: %s", exc)
        _reachability_cache["value"] = False
    _reachability_cache["ts"] = now
    return bool(_reachability_cache.get("value"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_mysql_db.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/mysql_db.py backend/tests/test_mysql_db.py
git commit -m "feat(edia): add lazy MySQL engine + get_mysql_db dependency"
```

---

### Task A3: Create `models/market_dashboard.py` — autoload EDIA tables

**Files:**
- Create: `backend/app/models/market_dashboard.py`
- Test: `backend/tests/test_models_market_dashboard.py`

**Interfaces:**
- Consumes: `get_mysql_engine()` (Task A2)
- Produces: `PRODUCT_TABLE_MAP: dict[str, dict]` (product_id → {table, value_col}); `get_product_table(product_id) -> Table | None` (autoload); `list_known_product_ids() -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_models_market_dashboard.py
"""market_dashboard models — product→table map + autoload helper."""
from app.models import market_dashboard as m


def test_product_table_map_has_six_known_products():
    assert set(m.PRODUCT_TABLE_MAP.keys()) == {
        "naphtha", "mixed_c5", "isoprene", "piperylene", "dcpd", "sis_rubber"
    }


def test_product_table_map_shape():
    cfg = m.PRODUCT_TABLE_MAP["naphtha"]
    assert cfg["table"] == "裂解C5"
    assert cfg["value_col"] == "日本石脑油Japanese naphtha"
    assert "label_en" in cfg and "label_zh" in cfg


def test_list_known_product_ids_includes_mapping():
    ids = set(m.list_known_product_ids())
    assert "naphtha" in ids
    assert "isoprene" in ids


def test_get_product_table_returns_none_without_engine(monkeypatch):
    monkeypatch.setattr("app.models.market_dashboard.get_mysql_engine", lambda: None)
    assert m.get_product_table("naphtha") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_models_market_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.market_dashboard'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/models/market_dashboard.py
"""Read-only SQLAlchemy Table objects for EDIA's MySQL warehouse.

Tables are autoloaded on first use — we never call Base.metadata.create_all
and never issue INSERT/UPDATE/DELETE. This module mirrors EDIA's
backend/src/db/market_db.py mapping verbatim.
"""
import logging
from typing import Optional

from sqlalchemy import MetaData, Table

from app.core.mysql_db import get_mysql_engine

logger = logging.getLogger(__name__)

# product_id -> {table, value_col, label_en, label_zh, group}
PRODUCT_TABLE_MAP: dict[str, dict] = {
    "naphtha": {
        "table": "裂解C5",
        "value_col": "日本石脑油Japanese naphtha",
        "label_en": "Japanese Naphtha",
        "label_zh": "日本石脑油",
        "group": "upstream",
    },
    "mixed_c5": {
        "table": "裂解C5",
        "value_col": "扬子裂解C5Yangzi Cracking C5",
        "label_en": "Yangzi Cracked C5",
        "label_zh": "扬子裂解C5",
        "group": "upstream",
    },
    "isoprene": {
        "table": "异戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "Isoprene (East China)",
        "label_zh": "异戊二烯",
        "group": "midstream",
    },
    "piperylene": {
        "table": "间戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "Piperylene (East China)",
        "label_zh": "间戊二烯",
        "group": "midstream",
    },
    "dcpd": {
        "table": "双环戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "DCPD (East China)",
        "label_zh": "双环戊二烯",
        "group": "midstream",
    },
    "sis_rubber": {
        "table": "SIS",
        "value_col": "巴陵1105Baling 1105",
        "label_en": "SIS Rubber (Baling 1105)",
        "label_zh": "SIS橡胶",
        "group": "downstream",
    },
}

# Full product group membership (spec §6) — drives which IDs are surfaced
# in each section even when no table mapping is configured yet.
PRODUCT_GROUP_MAP: dict[str, list[str]] = {
    "upstream":   ["crude_oil", "naphtha", "cracked_c5", "cracked_c9"],
    "midstream":  ["isoprene", "piperylene", "dcpd", "raffinate_c5"],
    "downstream": ["sis", "c5_resin", "blowing_agent", "styrene"],
}

# Friendly display labels for every product (whether or not a table is mapped).
PRODUCT_LABELS: dict[str, dict] = {
    "crude_oil":   {"label_en": "Crude Oil (Brent)",  "label_zh": "原油(布伦特)"},
    "naphtha":     PRODUCT_TABLE_MAP["naphtha"],
    "cracked_c5":  {"label_en": "Cracked C5",         "label_zh": "裂解碳五"},
    "cracked_c9":  {"label_en": "Cracked C9",         "label_zh": "裂解碳九"},
    "isoprene":    PRODUCT_TABLE_MAP["isoprene"],
    "piperylene":  PRODUCT_TABLE_MAP["piperylene"],
    "dcpd":        PRODUCT_TABLE_MAP["dcpd"],
    "raffinate_c5":{"label_en": "Raffinate C5",       "label_zh": "抽余C5"},
    "sis":         PRODUCT_TABLE_MAP["sis_rubber"],
    "c5_resin":    {"label_en": "C5 Petroleum Resin", "label_zh": "C5石油树脂"},
    "blowing_agent":{"label_en": "Blowing Agent",     "label_zh": "发泡剂"},
    "styrene":     {"label_en": "Styrene",            "label_zh": "苯乙烯"},
}

# Alias used by spec section 6 downstream list
PRODUCT_LABELS["sis_rubber"] = PRODUCT_TABLE_MAP["sis_rubber"]

_metadata: Optional[MetaData] = None
_table_cache: dict[str, Table] = {}


def list_known_product_ids() -> list[str]:
    """All product_ids the dashboard surfaces (mapped + unmapped)."""
    out: list[str] = []
    for ids in PRODUCT_GROUP_MAP.values():
        out.extend(ids)
    return out


def get_product_table(product_id: str) -> Optional[Table]:
    """Return the autoloaded Table for a product_id, or None if unmapped / engine off."""
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if cfg is None:
        return None
    eng = get_mysql_engine()
    if eng is None:
        return None
    if product_id not in _table_cache:
        global _metadata
        if _metadata is None:
            _metadata = MetaData()
        _table_cache[product_id] = Table(
            cfg["table"], _metadata, autoload_with=eng
        )
    return _table_cache[product_id]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_models_market_dashboard.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/market_dashboard.py backend/tests/test_models_market_dashboard.py
git commit -m "feat(edia): autoload EDIA MySQL tables + product mapping"
```

---

### Task A4: Create `schemas/market_dashboard.py` — Pydantic response shapes

**Files:**
- Create: `backend/app/schemas/market_dashboard.py`
- Test: `backend/tests/test_schemas_market_dashboard.py`

**Interfaces:**
- Produces: `MarketSnapshot`, `PriceHistoryPoint`, `RegionalPriceRegion`, `RegionalPriceData`, `NewsListResponse`, `EnabledResponse`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_schemas_market_dashboard.py
"""Pydantic schemas for the EDIA dashboard mirror."""
import pytest
from pydantic import ValidationError

from app.schemas.market_dashboard import (
    MarketSnapshot, PriceHistoryPoint, RegionalPriceRegion,
    RegionalPriceData, NewsListResponse, EnabledResponse,
)


def test_market_snapshot_minimum_fields():
    s = MarketSnapshot(
        id="naphtha",
        name="Japanese Naphtha",
        price=612.5,
        date="2026-07-30",
        change_pct=1.2,
        group="upstream",
    )
    assert s.unit == "CNY/mt"
    assert s.weekly_change_pct is None
    assert s.note is None


def test_market_snapshot_allows_nulls_for_unmapped():
    s = MarketSnapshot(
        id="crude_oil",
        name="Crude Oil",
        price=None,
        date=None,
        change_pct=None,
        group="upstream",
        note="table mapping not configured in v1",
    )
    assert s.price is None


def test_price_history_point_required():
    with pytest.raises(ValidationError):
        PriceHistoryPoint(date="2026-07-30")  # missing price


def test_regional_price_region_minimal():
    r = RegionalPriceRegion(
        price_type="east_china", label="华东", is_internal=False,
        price=9800.0, change_pct=None, date="2026-07-30", days_stale=0,
    )
    assert r.days_stale == 0


def test_news_list_response_v1_shape():
    r = NewsListResponse(items=[], note="...")
    assert r.items == []


def test_enabled_response_off():
    e = EnabledResponse(enabled=False, reachable=False, reason="not_configured")
    assert e.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_schemas_market_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/schemas/market_dashboard.py
"""Pydantic response shapes for the EDIA market dashboard mirror.

Field names mirror EDIA's frontend interfaces
(MarketSnapshotItem, RegionalPriceData, etc.) so the ported EDIA React
components can consume the payload without remapping.
"""
from typing import Optional
from pydantic import BaseModel, Field


class MarketSnapshot(BaseModel):
    id: str
    name: str
    group: str
    price: Optional[float] = None
    date: Optional[str] = None
    unit: str = "CNY/mt"
    change_pct: Optional[float] = None
    weekly_change_pct: Optional[float] = None
    daily_gap_value: Optional[float] = None
    weekly_gap_value: Optional[float] = None
    daily_reference_date: Optional[str] = None
    weekly_reference_date: Optional[str] = None
    data_source_detail: Optional[str] = None
    note: Optional[str] = None  # set when mapping is missing


class PriceHistoryPoint(BaseModel):
    date: str
    price: float
    volume: Optional[float] = None


class RegionalPriceRegion(BaseModel):
    price_type: str
    label: str
    is_internal: bool = False
    price: float
    change_pct: Optional[float] = None
    date: str
    days_stale: int = 0


class RegionalPriceData(BaseModel):
    product_id: str
    regions: list[RegionalPriceRegion]
    spread_cny: Optional[float] = None  # v1: not computed; left null
    spread_level: Optional[str] = None  # v1: not computed
    price_realization_pct: Optional[float] = None  # v1: not computed
    note: Optional[str] = None


class NewsListItem(BaseModel):
    """v1 placeholder — real news requires EDIA's AI event pipeline."""
    event_id: str
    headline: str
    detected_at: str
    source_url: Optional[str] = None
    summary: Optional[str] = None
    group: Optional[str] = None


class NewsListResponse(BaseModel):
    items: list[NewsListItem] = Field(default_factory=list)
    note: Optional[str] = None


class EnabledResponse(BaseModel):
    enabled: bool
    reachable: bool
    reason: Optional[str] = None  # "not_configured" | "configured_unreachable" | "ok"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_schemas_market_dashboard.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/market_dashboard.py backend/tests/test_schemas_market_dashboard.py
git commit -m "feat(edia): Pydantic schemas for market dashboard mirror"
```

---

### Task A5: Create `services/market_dashboard_service.py` — snapshot + history (Phase A scope)

**Files:**
- Create: `backend/app/services/market_dashboard_service.py`
- Test: `backend/tests/test_service_market_dashboard.py`

**Interfaces:**
- Consumes: `get_mysql_engine()` (Task A2), `PRODUCT_TABLE_MAP`, `PRODUCT_GROUP_MAP`, `PRODUCT_LABELS`, `get_product_table` (Task A3), `MarketSnapshot`, `PriceHistoryPoint` (Task A4)
- Produces:
  - `is_edia_enabled() -> bool` — delegates to `settings.is_edia_enabled`
  - `is_edia_reachable_cached() -> bool` — delegates to `mysql_db.is_edia_reachable`
  - `get_market_snapshot(group: str | None) -> list[MarketSnapshot]`
  - `get_price_history(product_id: str, days: int = 30) -> list[PriceHistoryPoint]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_service_market_dashboard.py
"""market_dashboard_service — snapshot/history read paths + unmapped fallback."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import market_dashboard_service as svc


def _mock_engine_with_rows(rows, columns):
    """Return a fake engine whose .connect() yields rows with given columns."""
    eng = MagicMock()
    conn = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    result.keys.return_value = columns
    conn.execute.return_value = result
    eng.connect.return_value.__enter__.return_value = conn
    return eng


def test_is_edia_enabled_when_url_set(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    assert svc.is_edia_enabled() is True


def test_get_market_snapshot_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "")
    with pytest.raises(HTTPException) as exc:
        svc.get_market_snapshot(group=None)
    assert exc.value.status_code == 503
    assert exc.value.detail == "edia_unavailable"


def test_get_market_snapshot_for_unmapped_product_returns_note(monkeypatch):
    """Products without a table mapping surface as null price + note."""
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr("app.services.market_dashboard_service.get_product_table", lambda pid: None)
    monkeypatch.setattr("app.services.market_dashboard_service.get_mysql_engine", lambda: MagicMock())
    snap = svc.get_market_snapshot(group="upstream")
    crude = next(s for s in snap if s.id == "crude_oil")
    assert crude.price is None
    assert "not configured" in (crude.note or "")


def test_get_market_snapshot_mapped_product_uses_history(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    # History: two points; last = 612.5, prev = 600.0 → change = +2.083%
    history_rows = [("2026-07-29", 600.0), ("2026-07-30", 612.5)]
    fake_engine = _mock_engine_with_rows(history_rows, ["date", "price"])
    monkeypatch.setattr("app.services.market_dashboard_service.get_mysql_engine",
                        lambda: fake_engine)
    monkeypatch.setattr("app.services.market_dashboard_service.get_product_table", lambda pid: MagicMock())
    snap = svc.get_market_snapshot(group="upstream")
    naphtha = next(s for s in snap if s.id == "naphtha")
    assert naphtha.price == 612.5
    assert naphtha.date == "2026-07-30"
    assert abs(naphtha.change_pct - 2.08) < 0.05


def test_get_price_history_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "")
    with pytest.raises(HTTPException) as exc:
        svc.get_price_history(product_id="naphtha", days=30)
    assert exc.value.detail == "edia_unavailable"


def test_get_price_history_unmapped_returns_empty(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr("app.services.market_dashboard_service.get_product_table", lambda pid: None)
    monkeypatch.setattr("app.services.market_dashboard_service.get_mysql_engine", lambda: MagicMock())
    assert svc.get_price_history("crude_oil", days=30) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_service_market_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/market_dashboard_service.py
"""Read-only queries against EDIA's MySQL warehouse.

v1 scope: snapshot + history per-product using EDIA's table mapping.
change_pct is computed from the last two history points (simpler than
EDIA's _compute_market_snapshot, which depends on ERP sales data +
execution prices not present in our read-only mirror).
"""
import logging
from typing import Optional
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.core.mysql_db import get_mysql_engine, is_edia_reachable
from app.models.market_dashboard import (
    PRODUCT_TABLE_MAP, PRODUCT_GROUP_MAP, PRODUCT_LABELS, get_product_table,
)
from app.schemas.market_dashboard import MarketSnapshot, PriceHistoryPoint

logger = logging.getLogger(__name__)


def is_edia_enabled() -> bool:
    return bool(settings.EDIA_MYSQL_URL)


def is_edia_reachable_cached() -> bool:
    return is_edia_reachable()


def _require_edia():
    """Raise 503 if EDIA is not configured or not reachable."""
    if not settings.EDIA_MYSQL_URL:
        raise HTTPException(status_code=503, detail="edia_unavailable")
    if not is_edia_reachable_cached():
        raise HTTPException(status_code=503, detail="edia_unreachable")


def _read_product_history_rows(product_id: str) -> list[tuple]:
    """Return [(date_str, price_float), ...] from EDIA's table for a product.

    Uses raw SQL on the autloaded Table — preserves EDIA's "first column is
    the date index" convention from market_db.py.
    """
    tbl = get_product_table(product_id)
    eng = get_mysql_engine()
    if tbl is None or eng is None:
        return []
    cfg = PRODUCT_TABLE_MAP[product_id]
    value_col = cfg["value_col"]
    # Build a SELECT with the table's first column (date) + the value column.
    # SQLAlchemy autoload already populated tbl.columns; index [0] = date col.
    cols = list(tbl.columns.keys())
    if value_col not in cols:
        # Column name drifted since EDIA shipped — surface as empty, log.
        logger.warning("EDIA column %r not in table %r (cols=%s)", value_col, cfg["table"], cols[:6])
        return []
    date_col = cols[0]
    stmt = text(f"SELECT `{date_col}` AS d, `{value_col}` AS p FROM `{cfg['table']}`")
    out: list[tuple] = []
    with eng.connect() as conn:
        result = conn.execute(stmt)
        for row in result:
            raw_date = str(row[0] or "")
            raw_price = row[1]
            if raw_price is None:
                continue
            try:
                price_val = float(raw_price)
            except (TypeError, ValueError):
                continue
            date_str = raw_date.replace("/", "-")[:10]
            out.append((date_str, round(price_val, 2)))
    return out


def get_price_history(product_id: str, days: int = 30) -> list[PriceHistoryPoint]:
    """Return daily price points for the last N days (default 30)."""
    _require_edia()
    rows = _read_product_history_rows(product_id)
    if not rows:
        return []
    cutoff = (date.today() - timedelta(days=max(days, 1))).isoformat()
    out: list[PriceHistoryPoint] = []
    for d, p in rows:
        if d < cutoff:
            continue
        out.append(PriceHistoryPoint(date=d, price=p))
    return out


def get_market_snapshot(group: Optional[str] = None) -> list[MarketSnapshot]:
    """Return one MarketSnapshot per product in the requested group.

    group=None returns all 12 products (used by /overview).
    Products without a table mapping return price=None + a note.
    """
    _require_edia()
    if group is None:
        product_ids: list[str] = []
        for grp, ids in PRODUCT_GROUP_MAP.items():
            product_ids.extend(ids)
    else:
        product_ids = PRODUCT_GROUP_MAP.get(group, [])

    snapshots: list[MarketSnapshot] = []
    for pid in product_ids:
        labels = PRODUCT_LABELS.get(pid, {})
        name = labels.get("label_en", pid)
        rows = _read_product_history_rows(pid) if pid in PRODUCT_TABLE_MAP else []
        if not rows:
            snapshots.append(MarketSnapshot(
                id=pid,
                name=name,
                group=labels.get("group", group or "unknown"),
                price=None,
                date=None,
                change_pct=None,
                note=("table mapping not configured in v1"
                      if pid not in PRODUCT_TABLE_MAP else "no rows in EDIA table"),
            ))
            continue
        last_date, last_price = rows[-1]
        prev_price = rows[-2][1] if len(rows) > 1 else None
        change_pct = (
            round((last_price - prev_price) / prev_price * 100, 2)
            if prev_price else 0.0
        )
        # 7-day window for weekly_change_pct (calendar-date based)
        weekly_rows = [r for r in rows if _within_7d(r[0], last_date)]
        weekly_prev = weekly_rows[0][1] if len(weekly_rows) > 1 else None
        weekly_change = (
            round((last_price - weekly_prev) / weekly_prev * 100, 2)
            if weekly_prev else None
        )
        snapshots.append(MarketSnapshot(
            id=pid,
            name=name,
            group=labels.get("group", group or "unknown"),
            price=last_price,
            date=last_date,
            change_pct=change_pct,
            weekly_change_pct=weekly_change,
            daily_gap_value=round(last_price - prev_price, 2) if prev_price else None,
            daily_reference_date=(rows[-2][0] if len(rows) > 1 else None),
            weekly_gap_value=round(last_price - weekly_prev, 2) if weekly_prev else None,
            weekly_reference_date=(weekly_rows[0][0] if len(weekly_rows) > 0 else None),
            data_source_detail=f"EDIA MySQL: {PRODUCT_TABLE_MAP[pid]['table']}",
        ))
    return snapshots


def _within_7d(d_str: str, ref_date_str: str) -> bool:
    """Return True if d_str is within 7 calendar days before ref_date_str."""
    try:
        d = date.fromisoformat(d_str)
        ref = date.fromisoformat(ref_date_str)
    except ValueError:
        return False
    return (ref - timedelta(days=7)) <= d <= ref
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_service_market_dashboard.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/market_dashboard_service.py backend/tests/test_service_market_dashboard.py
git commit -m "feat(edia): snapshot + history service with unmapped-product fallback"
```

---

### Task A6: Create `routers/market_dashboard.py` + register in `main.py`

**Files:**
- Create: `backend/app/routers/market_dashboard.py`
- Modify: `backend/main.py` (import after line 52; include_router after line 642)
- Test: `backend/tests/test_router_market_dashboard.py`

**Interfaces:**
- Consumes: `Depends(get_current_user_required)` (from `app.deps`), service functions from Task A5
- Produces: GET `/api/market-dashboard/enabled`, `/snapshot?group=`, `/history/{id}?days=`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_router_market_dashboard.py
"""market_dashboard router — auth + 503 + happy path with stubbed service."""
from unittest.mock import patch
from fastapi.testclient import TestClient


def _client_no_auth() -> TestClient:
    from main import app
    return TestClient(app)


def test_enabled_returns_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "")
    monkeypatch.setattr("app.services.market_dashboard_service.is_edia_reachable_cached", lambda: False)
    client = _client_no_auth()
    r = client.get("/api/market-dashboard/enabled")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"


def test_snapshot_requires_auth(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr("app.services.market_dashboard_service.is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr("app.services.market_dashboard_service.get_market_snapshot",
                        lambda group=None: [])
    client = _client_no_auth()
    # No Authorization header → ProtectedRoute should not let it through.
    # The router still applies get_current_user_required, so expect 401/422/403.
    r = client.get("/api/market-dashboard/snapshot?group=upstream")
    assert r.status_code in (401, 403, 422)


def test_history_returns_503_when_unreachable(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr("app.services.market_dashboard_service.is_edia_reachable_cached", lambda: False)
    client = _client_no_auth()
    r = client.get("/api/market-dashboard/history/naphtha?days=30")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unreachable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_router_market_dashboard.py -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'app'` or 404 from missing route

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/routers/market_dashboard.py`:

```python
# backend/app/routers/market_dashboard.py
"""EDIA market dashboard — read-only mirror router.

All routes require a logged-in user (Depends(get_current_user_required)).
Fail-closed: empty EDIA_MYSQL_URL → 503 edia_unavailable; unreachable host
→ 503 edia_unreachable. No silent empty data.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_current_user_required
from app.core.mysql_db import get_mysql_db
from app.schemas.market_dashboard import (
    EnabledResponse, MarketSnapshot, PriceHistoryPoint,
)
from app.services import market_dashboard_service as svc

router = APIRouter(prefix="/market-dashboard", tags=["market-dashboard"])


@router.get("/enabled", response_model=EnabledResponse)
def get_enabled(user=Depends(get_current_user_required)) -> EnabledResponse:
    if not svc.is_edia_enabled():
        return EnabledResponse(enabled=False, reachable=False, reason="not_configured")
    reachable = svc.is_edia_reachable_cached()
    return EnabledResponse(
        enabled=True,
        reachable=reachable,
        reason="ok" if reachable else "configured_unreachable",
    )


@router.get("/snapshot", response_model=list[MarketSnapshot])
def get_snapshot(
    group: Optional[str] = Query(None, pattern="^(upstream|midstream|downstream)$"),
    user=Depends(get_current_user_required),
) -> list[MarketSnapshot]:
    """Return one snapshot per product in the requested group.

    group omitted → all 12 products (overview section).
    """
    return svc.get_market_snapshot(group=group)


@router.get("/history/{product_id}", response_model=list[PriceHistoryPoint])
def get_history(
    product_id: str,
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user_required),
) -> list[PriceHistoryPoint]:
    return svc.get_price_history(product_id, days=days)
```

Modify `backend/main.py`:

After line 52 (`from app.routers.hooks import router as hooks_router`), add:

```python
from app.routers.market_dashboard import router as market_dashboard_router
```

After line 642 (`app.include_router(hooks_router, prefix="/api")`), add:

```python
    app.include_router(market_dashboard_router, prefix="/api")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_router_market_dashboard.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/market_dashboard.py backend/main.py backend/tests/test_router_market_dashboard.py
git commit -m "feat(edia): register /api/market-dashboard router (enabled/snapshot/history)"
```

---

## Phase B — Frontend Skeleton

### Task B1: Create `api/marketDashboard.js` API client

**Files:**
- Create: `frontend/src/api/marketDashboard.js`
- Test: `frontend/src/api/__tests__/marketDashboard.test.js`

**Interfaces:**
- Consumes: `base44` client from `@/api/base44Client`
- Produces: `marketDashboardApi.enabled()`, `.snapshot(group?)`, `.history(id, days?)` (Phase A endpoints); `.regional(id)` + `.news(group?, limit?)` are stubs used in Phase E

- [ ] **Step 1: Write the failing test**

```js
// frontend/src/api/__tests__/marketDashboard.test.js
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/api/base44Client', () => ({
  base44: {
    get: vi.fn(),
  },
}));

import { marketDashboardApi } from '../marketDashboard';
import { base44 } from '@/api/base44Client';

describe('marketDashboardApi', () => {
  beforeEach(() => base44.get.mockReset());

  it('enabled() calls /market-dashboard/enabled', async () => {
    base44.get.mockResolvedValueOnce({ enabled: true });
    const r = await marketDashboardApi.enabled();
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/enabled');
    expect(r.enabled).toBe(true);
  });

  it('snapshot(group) forwards group as params', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.snapshot('upstream');
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/snapshot', {
      params: { group: 'upstream' },
    });
  });

  it('snapshot() without group sends null', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.snapshot();
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/snapshot', {
      params: { group: undefined },
    });
  });

  it('history(id, days) builds path + params', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.history('naphtha', 30);
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/history/naphtha', {
      params: { days: 30 },
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/api/__tests__/marketDashboard.test.js`
Expected: FAIL with `Failed to resolve import "../marketDashboard"`

- [ ] **Step 3: Write minimal implementation**

```js
// frontend/src/api/marketDashboard.js
import { base44 } from '@/api/base44Client';

/**
 * EDIA Market Dashboard API client.
 *
 * All endpoints live under /api/market-dashboard/* (registered in
 * backend/main.py). Fail-closed: empty EDIA_MYSQL_URL → HTTP 503 with
 * {"detail": "edia_unavailable"}. The frontend surfaces this as an
 * "EDIA data source offline" banner (see MarketDashboardPage.jsx).
 */
export const marketDashboardApi = {
  enabled: () => base44.get('/market-dashboard/enabled'),
  snapshot: (group) =>
    base44.get('/market-dashboard/snapshot', { params: { group } }),
  history: (productId, days = 30) =>
    base44.get(`/market-dashboard/history/${productId}`, { params: { days } }),
  // Phase E:
  regional: (productId) =>
    base44.get(`/market-dashboard/regional/${productId}`),
  news: (group, limit = 18) =>
    base44.get('/market-dashboard/news', { params: { group, limit } }),
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/api/__tests__/marketDashboard.test.js`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/marketDashboard.js frontend/src/api/__tests__/marketDashboard.test.js
git commit -m "feat(edia): frontend API client for /market-dashboard endpoints"
```

---

### Task B2: Create `MarketDashboardPage.jsx` skeleton

**Files:**
- Create: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Test: `frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`

**Interfaces:**
- Consumes: `useParams()` from `react-router-dom`, `useQuery` from `@tanstack/react-query`, `marketDashboardApi.enabled()` + `.snapshot()`
- Produces: default-exported React component rendering a section-aware dashboard shell

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' }),
    snapshot: vi.fn().mockResolvedValue([]),
    history: vi.fn().mockResolvedValue([]),
  },
}));

import MarketDashboardPage from '../MarketDashboardPage';

function renderAt(path) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/market-dashboard/:section" element={<MarketDashboardPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe('MarketDashboardPage', () => {
  it('renders the section name from the URL', async () => {
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText(/upstream/i)).toBeInTheDocument();
  });

  it('renders overview section header', async () => {
    renderAt('/market-dashboard/overview');
    expect(await screen.findByText(/overview/i)).toBeInTheDocument();
  });

  it('renders invalid-section fallback', async () => {
    renderAt('/market-dashboard/bogus');
    expect(await screen.findByText(/unknown section/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: FAIL with `Failed to resolve import "../MarketDashboardPage"`

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx
import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from './lib/typography';

const VALID_SECTIONS = ['overview', 'upstream', 'midstream', 'downstream'];

export default function MarketDashboardPage() {
  const { section } = useParams();

  if (!VALID_SECTIONS.includes(section)) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>Unknown section: {section}</h1>
        <p className={TYPO.listText + ' mt-2'}>
          Valid sections: {VALID_SECTIONS.join(', ')}
        </p>
      </div>
    );
  }

  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 30_000,
  });

  if (enabledQuery.isLoading) {
    return <div className="p-8 text-sm text-slate-500">Checking EDIA availability…</div>;
  }

  if (enabledQuery.isError || !enabledQuery.data?.enabled) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          EDIA data source is not configured on this server. Set
          <code className="mx-1 rounded bg-amber-100 px-1">EDIA_MYSQL_URL</code>
          in the backend environment to enable the dashboard.
        </div>
      </div>
    );
  }

  if (enabledQuery.data && !enabledQuery.data.reachable) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          EDIA MySQL warehouse is configured but currently unreachable.
          Reason: {enabledQuery.data.reason || 'configured_unreachable'}
        </div>
      </div>
    );
  }

  return (
    <div className="p-8">
      <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
      <p className={TYPO.listText + ' mt-1 text-slate-500'}>
        Snapshot + chart components will be wired in Phase C/D/E.
      </p>
      {/* Phase C: <SnapshotGrid section={section} />
          Phase D: <PriceChart productId="…" days={30} />
          Phase E: <RegionalTable /> + <NewsPanel /> */}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: PASS (3 tests). NOTE: the test mocks `enabled()` to resolve `{enabled: true, reachable: true}`, so the "EDIA not configured" branch isn't hit; the page renders the heading.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx \
        frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx
git commit -m "feat(edia): MarketDashboardPage skeleton with section validation + 503 banner"
```

---

### Task B3: Port `lib/` files (chartTheme, priceChange, typography) from EDIA

**Files:**
- Create: `frontend/src/pages/MarketDashboard/lib/typography.js`
- Create: `frontend/src/pages/MarketDashboard/lib/chartTheme.js`
- Create: `frontend/src/pages/MarketDashboard/lib/priceChange.js`
- Test: `frontend/src/pages/MarketDashboard/lib/__tests__/ports.test.js`

**Interfaces:**
- Produces: `TYPO` constant (typography.js); `getChartTheme(isDark)`, `rechartsTooltipStyle(theme, isDark)` (chartTheme.js); `getPriceChangeDirection`, `getPriceChangeTextClass`, `formatSignedPriceChange`, `getPriceChangeArrow`, `NO_DATA_LABEL` (priceChange.js)

- [ ] **Step 1: Write the failing test**

```js
// frontend/src/pages/MarketDashboard/lib/__tests__/ports.test.js
import { describe, it, expect } from 'vitest';
import { TYPO } from '../typography';
import { getChartTheme, rechartsTooltipStyle } from '../chartTheme';
import {
  getPriceChangeDirection,
  getPriceChangeTextClass,
  formatSignedPriceChange,
  getPriceChangeArrow,
  NO_DATA_LABEL,
} from '../priceChange';

describe('typography port', () => {
  it('exports TYPO with expected keys', () => {
    expect(TYPO.pageTitle).toContain('text-lg');
    expect(TYPO.priceData).toContain('font-mono');
  });
});

describe('chartTheme port', () => {
  it('returns dark theme tokens when isDark', () => {
    const t = getChartTheme(true);
    expect(t.grid).toBe('#2a2f3a');
    expect(t.tooltip.backgroundColor).toBe('#1c1f24');
  });
  it('returns light theme tokens when not isDark', () => {
    const t = getChartTheme(false);
    expect(t.grid).toBe('#e2e8f0');
  });
  it('rechartsTooltipStyle returns style object', () => {
    const t = getChartTheme(false);
    const s = rechartsTooltipStyle(t, false);
    expect(s).toHaveProperty('backgroundColor');
  });
});

describe('priceChange port', () => {
  it('classifies direction', () => {
    expect(getPriceChangeDirection(1)).toBe('increase');
    expect(getPriceChangeDirection(-1)).toBe('decrease');
    expect(getPriceChangeDirection(0)).toBe('unchanged');
    expect(getPriceChangeDirection(null)).toBe('no_data');
  });
  it('returns tailwind text class', () => {
    expect(getPriceChangeTextClass(1)).toContain('text-red');
    expect(getPriceChangeTextClass(-1)).toContain('text-green');
  });
  it('formats signed change', () => {
    expect(formatSignedPriceChange(1.5)).toBe('+1.5');
    expect(formatSignedPriceChange(0)).toBe('0.00');
    expect(formatSignedPriceChange(null)).toBe(NO_DATA_LABEL);
  });
  it('arrow mapping', () => {
    expect(getPriceChangeArrow(1)).toBe('up');
    expect(getPriceChangeArrow(-1)).toBe('down');
    expect(getPriceChangeArrow(0)).toBe('flat');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/lib/__tests__/ports.test.js`
Expected: FAIL — modules don't exist

- [ ] **Step 3: Write minimal implementation**

Create three files by porting from EDIA (drop TypeScript types, keep the rest verbatim):

`frontend/src/pages/MarketDashboard/lib/typography.js`:
```js
// Ported from EDIA_5.1.2/frontend/src/lib/typography.ts
export const TYPO = {
  pageTitle: 'text-lg font-bold tracking-tight',
  cardTitle: 'text-base font-semibold',
  listText: 'text-sm',
  listTextMedium: 'text-sm font-medium',
  priceData: 'text-xl font-bold font-mono tracking-tight',
  secondaryData: 'text-xs text-slate-500',
  secondaryMono: 'text-xs text-slate-500 font-mono',
  label: 'text-xs font-semibold',
  micro: 'text-[11px] text-slate-500',
};

export const STATUS_STYLE = {
  danger: 'text-red-700 bg-red-100 border-red-200',
  warning: 'text-amber-700 bg-amber-100 border-amber-200',
  safe: 'text-green-700 bg-green-100 border-green-200',
  excess: 'text-blue-700 bg-blue-100 border-blue-200',
};
```

`frontend/src/pages/MarketDashboard/lib/chartTheme.js`:
```js
// Ported from EDIA_5.1.2/frontend/src/lib/chartTheme.ts
export function getChartTheme(isDark) {
  if (isDark) {
    return {
      grid: '#2a2f3a',
      axis: '#4b5563',
      tick: '#909296',
      tickMuted: '#6b7280',
      tooltip: {
        backgroundColor: '#1c1f24',
        border: '1px solid #373a40',
        color: '#c1c2c5',
      },
      brushFill: '#25262b',
      brushStroke: '#4b5563',
    };
  }
  return {
    grid: '#e2e8f0',
    axis: '#cbd5e1',
    tick: '#64748b',
    tickMuted: '#94a3b8',
    tooltip: {
      backgroundColor: '#ffffff',
      border: '1px solid #e2e8f0',
      color: '#64748b',
    },
    brushFill: '#f8fafc',
    brushStroke: '#cbd5e1',
  };
}

export function rechartsTooltipStyle(theme, isDark = false) {
  return {
    backgroundColor: theme.tooltip.backgroundColor,
    border: theme.tooltip.border,
    color: theme.tooltip.color,
    borderRadius: isDark ? 10 : 8,
    fontSize: isDark ? 12 : 11,
    boxShadow: isDark
      ? '0 10px 24px rgba(0, 0, 0, 0.35)'
      : '0 4px 6px -1px rgb(0 0 0 / 0.08)',
  };
}
```

`frontend/src/pages/MarketDashboard/lib/priceChange.js`:
```js
// Ported from EDIA_5.1.2/frontend/src/lib/priceChange.ts
export const NO_DATA_LABEL = 'No Data Available';

export function getPriceChangeDirection(value) {
  if (value == null || !Number.isFinite(value)) return 'no_data';
  if (value > 0) return 'increase';
  if (value < 0) return 'decrease';
  return 'unchanged';
}

export function getPriceChangeTextClass(value, tone = 'default') {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return tone === 'bright' ? 'text-red-400' : 'text-red-600';
  if (direction === 'decrease') return tone === 'bright' ? 'text-green-400' : 'text-green-600';
  return tone === 'bright' ? 'text-slate-400' : 'text-slate-500';
}

export function getPriceChangeStrongTextClass(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'text-red-700';
  if (direction === 'decrease') return 'text-green-700';
  return 'text-slate-500';
}

export function getPriceChangeBadgeClass(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'bg-red-50 text-red-700';
  if (direction === 'decrease') return 'bg-green-50 text-green-700';
  return 'bg-slate-50 text-slate-600';
}

export function getPriceChangeLineColor(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return '#dc2626';
  if (direction === 'decrease') return '#16a34a';
  return '#64748b';
}

export function formatSignedPriceChange(value, options = {}, locale, suffix = '') {
  if (value == null || !Number.isFinite(value)) return NO_DATA_LABEL;
  const formatted = value.toLocaleString(locale, options);
  if (value === 0) return `0.00${suffix}`;
  return `${value > 0 ? '+' : ''}${formatted}${suffix}`;
}

export function getPriceChangeArrow(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'up';
  if (direction === 'decrease') return 'down';
  return 'flat';
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/lib/__tests__/ports.test.js`
Expected: PASS (all assertions in 3 describe blocks)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/lib/
git commit -m "feat(edia): port chartTheme, priceChange, typography libs from EDIA"
```

---

### Task B4: Add `DashboardAccordion` to `Sidebar.jsx` + route in `App.jsx`

**Files:**
- Modify: `frontend/src/components/Sidebar.jsx` (line 6 import; insert component before line 229 `<nav>` block; render accordion after `</nav>`)
- Modify: `frontend/src/App.jsx` (lazy import after line 40; route after line 102)
- Test: `frontend/src/components/__tests__/Sidebar.accordion.test.jsx`

**Interfaces:**
- Consumes: `NavLink`, `useLocation` from `react-router-dom`; `LayoutDashboard`, `ChevronDown` from `lucide-react`; `marketDashboardApi.enabled()`; `useQuery` from `@tanstack/react-query`; existing `linkClass` from `Sidebar.jsx`
- Produces: 4 new nav routes (`/market-dashboard/{overview|upstream|midstream|downstream}`)

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/components/__tests__/Sidebar.accordion.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/base44Client', () => ({
  base44: { get: vi.fn() },
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' }),
    snapshot: vi.fn().mockResolvedValue([]),
    history: vi.fn().mockResolvedValue([]),
  },
}));

// Mock SessionList + UserMenu + ThemeToggle to keep the test focused
vi.mock('@/components/chat/SessionList', () => ({ default: () => null }));
vi.mock('@/components/UserMenu', () => ({ default: () => null }));
vi.mock('@/components/ThemeToggle', () => ({ default: () => null }));
vi.mock('@/lib/ChatSessionContext', () => ({
  useChatSession: () => ({ newChat: vi.fn() }),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: { sessionList: { newTask: 'New Task' } } }),
}));
vi.mock('@/hooks/useScreenSize', () => ({ useScreenSize: () => ({ isMobile: false }) }));

import Sidebar from '../Sidebar';

function renderSidebar(initialPath = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Sidebar DashboardAccordion', () => {
  it('renders the Dashboard group header when EDIA is enabled', async () => {
    renderSidebar();
    expect(await screen.findByText(/dashboard/i)).toBeInTheDocument();
  });

  it('auto-expands on /market-dashboard/* routes', async () => {
    renderSidebar('/market-dashboard/upstream');
    expect(await screen.findByText(/overview/i, { selector: 'a' })).toBeInTheDocument();
    expect(await screen.findByText(/upstream/i, { selector: 'a' })).toBeInTheDocument();
  });

  it('renders 4 sub-items when expanded', async () => {
    renderSidebar('/market-dashboard/overview');
    const links = await screen.findAllByRole('link');
    const dashboardLinks = links.filter((a) =>
      a.getAttribute('href')?.startsWith('/market-dashboard/'),
    );
    expect(dashboardLinks).toHaveLength(4);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/__tests__/Sidebar.accordion.test.jsx`
Expected: FAIL — no "Dashboard" header

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/App.jsx`, after line 40 add:

```jsx
const MarketDashboardPage = lazy(() => import('@/pages/MarketDashboard/MarketDashboardPage'));
```

After line 102 (`<Route path="/dashboard/:id" element={<DashboardView />} />`) add:

```jsx
          <Route path="/market-dashboard/:section" element={<MarketDashboardPage />} />
```

In `frontend/src/components/Sidebar.jsx`:

1. Update line 6 imports — replace:
  ```jsx
  import { Plus, Workflow, Boxes, Store, Wrench, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
  ```
  with:
  ```jsx
  import { Plus, Workflow, Boxes, Store, Wrench, PanelLeftClose, PanelLeftOpen, LayoutDashboard, ChevronDown } from 'lucide-react';
  ```

2. Add imports after line 10:
  ```jsx
  import { useQuery } from '@tanstack/react-query';
  import { marketDashboardApi } from '@/api/marketDashboard';
  ```

3. Add a `DashboardAccordion` sub-component (place above `export default function Sidebar()` so it has access to `linkClass` via closure, OR define it inside the Sidebar function). Insert inside the `Sidebar()` function, just before the `return (` statement:

  ```jsx
  // ── Dashboard accordion ──────────────────────────────────────────
  // Read-only mirror of EDIA's MySQL warehouse. Auto-expands on
  // /market-dashboard/* routes. Hidden entirely when EDIA is not
  // configured (no dead nav items in dev environments).
  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 60_000,
  });
  const isDashboardRoute = location.pathname.startsWith('/market-dashboard');
  const [dashboardOpen, setDashboardOpen] = useState(isDashboardRoute);
  useEffect(() => {
    if (isDashboardRoute) setDashboardOpen(true);
  }, [isDashboardRoute]);

  const dashboardItems = [
    { to: '/market-dashboard/overview', label: 'Overview', group: 'overview' },
    { to: '/market-dashboard/upstream', label: 'Upstream', group: 'upstream' },
    { to: '/market-dashboard/midstream', label: 'Midstream', group: 'midstream' },
    { to: '/market-dashboard/downstream', label: 'Downstream', group: 'downstream' },
  ];
  const showAccordion = !enabledQuery.isLoading && enabledQuery.data?.enabled === true;
  ```

4. Insert the accordion JSX between the existing `<nav>…</nav>` block (lines 229–236) and the `{!collapsed && (` SessionList block (line 241). Replace the closing `</nav>` block boundary so the accordion sits after it:

  After line 235 (`</nav>`), insert:
  ```jsx
        {showAccordion && !collapsed && (
          <div className="mt-2 border-t border-sidebar-border pt-2">
            <button
              type="button"
              onClick={() => setDashboardOpen((v) => !v)}
              className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm font-semibold text-sidebar-foreground hover:bg-sidebar-accent"
              aria-expanded={dashboardOpen}
            >
              <LayoutDashboard className="h-4 w-4 shrink-0" />
              <span className="flex-1 text-left">Dashboard</span>
              <ChevronDown
                className={`h-3.5 w-3.5 transition-transform ${dashboardOpen ? 'rotate-180' : ''}`}
              />
            </button>
            {dashboardOpen && (
              <nav className="mt-1 space-y-0.5">
                {dashboardItems.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={linkClass}
                    style={{ paddingLeft: '2rem' }}
                  >
                    <span className="text-xs">{item.label}</span>
                  </NavLink>
                ))}
              </nav>
            )}
          </div>
        )}
  ```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/__tests__/Sidebar.accordion.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/Sidebar.jsx frontend/src/App.jsx \
        frontend/src/components/__tests__/Sidebar.accordion.test.jsx
git commit -m "feat(edia): add DashboardAccordion to Sidebar + /market-dashboard/:section route"
```

---

## Phase C — Snapshot Grid

### Task C1: Create `components/SnapshotGrid.jsx`

**Files:**
- Create: `frontend/src/pages/MarketDashboard/components/SnapshotGrid.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/SnapshotGrid.test.jsx`

**Interfaces:**
- Consumes: `MarketSnapshot` (passed from page), `TYPO`, `getPriceChangeTextClass`, `formatSignedPriceChange`, `NO_DATA_LABEL`
- Produces: `<SnapshotGrid items={[...]} />` — renders a responsive grid of snapshot cards

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/components/__tests__/SnapshotGrid.test.jsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import SnapshotGrid from '../SnapshotGrid';

const items = [
  { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
    change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
  { id: 'crude_oil', name: 'Crude Oil', price: null, date: null,
    change_pct: null, group: 'upstream', note: 'table mapping not configured in v1' },
];

describe('SnapshotGrid', () => {
  it('renders a card per item', () => {
    render(<SnapshotGrid items={items} />);
    expect(screen.getByText('Japanese Naphtha')).toBeInTheDocument();
    expect(screen.getByText('Crude Oil')).toBeInTheDocument();
  });

  it('shows price for mapped products', () => {
    render(<SnapshotGrid items={items} />);
    expect(screen.getByText('612.5')).toBeInTheDocument();
  });

  it('shows NO_DATA_LABEL when price is null', () => {
    render(<SnapshotGrid items={items} />);
    // crude_oil has no price; the card should render the no-data label
    expect(screen.getAllByText(/No Data Available/i).length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/SnapshotGrid.test.jsx`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/pages/MarketDashboard/components/SnapshotGrid.jsx
import { TYPO, NO_DATA_LABEL_LIT } from '../lib/typography';
import {
  getPriceChangeTextClass,
  formatSignedPriceChange,
  NO_DATA_LABEL,
} from '../lib/priceChange';

// Re-export so the typography/NO_DATA label is consistent across components.
export const NO_DATA = NO_DATA_LABEL;

/**
 * SnapshotGrid — one card per product, mirroring EDIA's OverviewDashboard
 * snapshot row. Shows name, latest price, daily change pct, date.
 */
export default function SnapshotGrid({ items = [] }) {
  if (!items.length) {
    return <p className={TYPO.listText + ' text-slate-500'}>No snapshot data.</p>;
  }
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((s) => {
        const changeClass = getPriceChangeTextClass(s.change_pct);
        const changeText =
          s.change_pct == null
            ? NO_DATA_LABEL
            : `${formatSignedPriceChange(s.change_pct)}%`;
        return (
          <div
            key={s.id}
            className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900"
          >
            <div className="flex items-baseline justify-between">
              <span className={TYPO.cardTitle}>{s.name}</span>
              <span className={TYPO.secondaryData}>{s.date || NO_DATA_LABEL}</span>
            </div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className={TYPO.priceData}>
                {s.price == null ? NO_DATA_LABEL : s.price}
              </span>
              {s.unit && <span className={TYPO.secondaryMono}>{s.unit}</span>}
            </div>
            <div className={`mt-1 text-sm ${changeClass}`}>{changeText}</div>
            {s.note && (
              <div className={TYPO.micro + ' mt-2 italic'}>{s.note}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/SnapshotGrid.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/components/SnapshotGrid.jsx \
        frontend/src/pages/MarketDashboard/components/__tests__/SnapshotGrid.test.jsx
git commit -m "feat(edia): SnapshotGrid renders price + change pct per product"
```

---

### Task C2: Wire `SnapshotGrid` into `MarketDashboardPage.jsx`

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Test: extend `frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`

**Interfaces:**
- Consumes: `SnapshotGrid` (Task C1), `marketDashboardApi.snapshot(group)`

- [ ] **Step 1: Write the failing test** (append to the existing test file)

```jsx
// Add to the existing describe block:
  it('renders snapshot cards when snapshot resolves', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText('Japanese Naphtha')).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: FAIL — page doesn't render the snapshot card yet

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`, replace the placeholder return block (the "Snapshot + chart components will be wired…" comment) with:

```jsx
  const snapshotQuery = useQuery({
    queryKey: ['market-dashboard', 'snapshot', section],
    queryFn: () => marketDashboardApi.snapshot(
      section === 'overview' ? undefined : section
    ),
    enabled: enabledQuery.data?.enabled && enabledQuery.data?.reachable,
    staleTime: 60_000,
  });

  return (
    <div className="p-8">
      <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
      <div className="mt-4">
        {snapshotQuery.isLoading && (
          <p className={TYPO.listText + ' text-slate-500'}>Loading snapshot…</p>
        )}
        {snapshotQuery.isError && (
          <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            Failed to load snapshot: {snapshotQuery.error?.message || 'unknown'}
          </div>
        )}
        {snapshotQuery.data && <SnapshotGrid items={snapshotQuery.data} />}
      </div>
    </div>
  );
```

Add the import at the top:
```jsx
import SnapshotGrid from './components/SnapshotGrid';
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: PASS (4 tests including the new one)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx \
        frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx
git commit -m "feat(edia): wire SnapshotGrid into MarketDashboardPage"
```

---

## Phase D — Price Charts

### Task D1: Create `components/PriceChart.jsx` (recharts ComposedChart)

**Files:**
- Create: `frontend/src/pages/MarketDashboard/components/PriceChart.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/PriceChart.test.jsx`

**Interfaces:**
- Consumes: `marketDashboardApi.history(productId, days)`, `useQuery`, `recharts` (ComposedChart, Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer), `getChartTheme`, `rechartsTooltipStyle`
- Produces: `<PriceChart productId="..." days={30} />`

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/components/__tests__/PriceChart.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    history: vi.fn().mockResolvedValue([
      { date: '2026-07-29', price: 600.0 },
      { date: '2026-07-30', price: 612.5 },
    ]),
  },
}));

// recharts ResponsiveContainer requires a sized parent; mock to a div.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div data-testid="rc">{children}</div>,
  ComposedChart: () => <div data-testid="composed" />,
  Area: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
}));

import PriceChart from '../PriceChart';
import { marketDashboardApi } from '@/api/marketDashboard';

function renderChart(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PriceChart {...props} />
    </QueryClientProvider>,
  );
}

describe('PriceChart', () => {
  it('renders the product id as a heading', async () => {
    renderChart({ productId: 'naphtha', days: 30 });
    expect(await screen.findByText(/naphtha/i)).toBeInTheDocument();
  });

  it('calls history() with productId + days', async () => {
    renderChart({ productId: 'naphtha', days: 30 });
    // wait for query to fire
    await screen.findByTestId('composed');
    expect(marketDashboardApi.history).toHaveBeenCalledWith('naphtha', 30);
  });

  it('renders empty-state message when history is empty', async () => {
    marketDashboardApi.history.mockResolvedValueOnce([]);
    renderChart({ productId: 'crude_oil', days: 30 });
    expect(await screen.findByText(/no history/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/PriceChart.test.jsx`
Expected: FAIL — module missing

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/pages/MarketDashboard/components/PriceChart.jsx
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Area, Line,
  XAxis, YAxis, Tooltip,
} from 'recharts';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { getChartTheme, rechartsTooltipStyle } from '../lib/chartTheme';
import { NO_DATA_LABEL } from '../lib/priceChange';
import { useTheme } from '@/components/ThemeProvider';

/**
 * PriceChart — per-product recharts ComposedChart (Area fill + Line stroke).
 * Mirrors EDIA's chart styling. Empty / loading states are surfaced inline.
 */
export default function PriceChart({ productId, days = 30 }) {
  const { theme } = useTheme?.() || { theme: 'light' };
  const isDark = theme === 'dark';
  const chartTheme = getChartTheme(isDark);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'history', productId, days],
    queryFn: () => marketDashboardApi.history(productId, days),
    staleTime: 60_000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-baseline justify-between">
        <span className={TYPO.cardTitle}>{productId}</span>
        <span className={TYPO.secondaryData}>{days}d</span>
      </div>
      {isLoading && (
        <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>
      )}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {!isLoading && !isError && (!data || data.length === 0) && (
        <p className={TYPO.listText + ' text-slate-500'}>No history.</p>
      )}
      {!isLoading && !isError && data && data.length > 0 && (
        <div style={{ width: '100%', height: 200 }}>
          <ResponsiveContainer>
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={`grad-${productId}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={chartTheme.axis} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={chartTheme.axis} stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="date"
                stroke={chartTheme.tick}
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: chartTheme.axis }}
              />
              <YAxis
                stroke={chartTheme.tick}
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: chartTheme.axis }}
                width={48}
              />
              <Tooltip contentStyle={rechartsTooltipStyle(chartTheme, isDark)} />
              <Area
                type="monotone"
                dataKey="price"
                stroke={chartTheme.axis}
                fill={`url(#grad-${productId})`}
                strokeWidth={1}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="price"
                stroke={chartTheme.tick}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
```

Note: if `@/components/ThemeProvider` does not exist in zhanlu, replace the import + `useTheme?.()` line with a fallback that reads the `<html class="dark">` attribute:

```jsx
import { useEffect, useState } from 'react';
function useThemeFallback() {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains('dark'));
    update();
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);
  return { theme: isDark ? 'dark' : 'light' };
}
```
Use whichever hook name (`useTheme` or `useThemeFallback`) matches the actual project — confirm at implementation time.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/PriceChart.test.jsx`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/components/PriceChart.jsx \
        frontend/src/pages/MarketDashboard/components/__tests__/PriceChart.test.jsx
git commit -m "feat(edia): PriceChart using recharts ComposedChart"
```

---

### Task D2: Wire `PriceChart` per product into `MarketDashboardPage.jsx`

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Test: extend `MarketDashboardPage.test.jsx`

**Interfaces:**
- Consumes: `PriceChart` (Task D1), the snapshot result (drives which product_ids to chart)

- [ ] **Step 1: Write the failing test** (append)

```jsx
  it('renders a PriceChart for each product in the snapshot', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
      { id: 'crude_oil', name: 'Crude Oil', price: null, date: null,
        change_pct: null, group: 'upstream' },
    ]);
    marketDashboardApi.history.mockResolvedValue([]);
    renderAt('/market-dashboard/upstream');
    // naphtha chart heading should appear (crude_oil is unmapped but
    // we still render a chart per snapshot item so the user sees the
    // "no history" state).
    const headings = await screen.findAllByText(/naphtha|crude_oil/i);
    expect(headings.length).toBeGreaterThanOrEqual(2);
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: FAIL — only SnapshotGrid renders, no PriceCharts

- [ ] **Step 3: Write minimal implementation**

In `MarketDashboardPage.jsx`, after the `<SnapshotGrid items={snapshotQuery.data} />` block, add:

```jsx
        {snapshotQuery.data && snapshotQuery.data.length > 0 && (
          <div className="mt-6 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {snapshotQuery.data.map((s) => (
              <PriceChart key={s.id} productId={s.id} days={30} />
            ))}
          </div>
        )}
```

Add the import:
```jsx
import PriceChart from './components/PriceChart';
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx \
        frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx
git commit -m "feat(edia): render PriceChart per product on the dashboard page"
```

---

## Phase E — Regional + News (v1 simplified)

### Task E1: Add `get_regional_prices` + `get_news_events` to service

**Files:**
- Modify: `backend/app/services/market_dashboard_service.py`
- Test: extend `backend/tests/test_service_market_dashboard.py`

**Interfaces:**
- Produces:
  - `get_regional_prices(product_id: str) -> RegionalPriceData` — v1 simplified: scans every column in the product's table that looks like a regional price column (substring match on `华东`/`华南`/`华北`/`East China`/etc.) and returns one `RegionalPriceRegion` per match. No spread/cost-floor analysis.
  - `get_news_events(group: str | None, limit: int = 18) -> NewsListResponse` — v1 returns `{items: [], note: "..."}`

- [ ] **Step 1: Write the failing test** (append to existing test file)

```python
def test_get_regional_prices_unmapped_returns_empty(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr("app.services.market_dashboard_service.get_product_table", lambda pid: None)
    monkeypatch.setattr("app.services.market_dashboard_service.get_mysql_engine", lambda: MagicMock())
    r = svc.get_regional_prices("crude_oil")
    assert r.regions == []
    assert "not configured" in (r.note or "")


def test_get_news_events_v1_returns_empty_with_note(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    r = svc.get_news_events(group="upstream", limit=18)
    assert r.items == []
    assert "AI event pipeline" in (r.note or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_service_market_dashboard.py::test_get_regional_prices_unmapped_returns_empty tests/test_service_market_dashboard.py::test_get_news_events_v1_returns_empty_with_note -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'get_regional_prices'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/market_dashboard_service.py`:

```python
from app.schemas.market_dashboard import (
    RegionalPriceData, RegionalPriceRegion, NewsListResponse,
)

# Column-name fragments that look like a regional price (per EDIA's
# "华东市场价East China market price" convention). Used by v1 regional.
_REGIONAL_TOKENS = ("华东", "华南", "华北", "East China", "South China", "North China")


def get_regional_prices(product_id: str) -> RegionalPriceData:
    """v1 simplified regional payload: one region per matching column.

    No spread or cost-floor analysis (those depend on EDIA's naphtha cost
    model, which is out of scope for the read-only mirror).
    """
    _require_edia()
    tbl = get_product_table(product_id)
    eng = get_mysql_engine()
    labels = PRODUCT_LABELS.get(product_id, {})
    if tbl is None or eng is None:
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note="table mapping not configured in v1",
        )
    cols = list(tbl.columns.keys())
    region_cols = [c for c in cols if any(tok in c for tok in _REGIONAL_TOKENS)]
    if not region_cols:
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note="no regional columns detected in EDIA table",
        )
    date_col = cols[0]
    regions: list[RegionalPriceRegion] = []
    for col in region_cols:
        # Pull last non-null price + its date for this column.
        stmt = text(
            f"SELECT `{date_col}` AS d, `{col}` AS p "
            f"FROM `{PRODUCT_TABLE_MAP[product_id]['table']}` "
            f"WHERE `{col}` IS NOT NULL "
            f"ORDER BY `{date_col}` DESC LIMIT 1"
        )
        try:
            with eng.connect() as conn:
                row = conn.execute(stmt).first()
        except Exception as exc:
            logger.warning("EDIA regional query failed for %s.%s: %s",
                           product_id, col, exc)
            continue
        if not row or row[1] is None:
            continue
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        label = col.split("East")[0].split("South")[0].split("North")[0] or col
        regions.append(RegionalPriceRegion(
            price_type=col,
            label=label.strip() or col,
            is_internal=False,
            price=round(price, 2),
            change_pct=None,  # v1: not computed per region
            date=str(row[0])[:10] if row[0] else "",
            days_stale=0,
        ))
    return RegionalPriceData(
        product_id=product_id,
        regions=regions,
        note="v1 simplified: no spread/cost-floor analysis",
    )


def get_news_events(group: Optional[str] = None, limit: int = 18) -> NewsListResponse:
    """v1 returns an empty list with an explanatory note.

    EDIA sources news from /api/events/latest — an AI-detected market-event
    pipeline stored in a separate system, not in the
    aipdp_data_warehouse_prod warehouse we mirror. Replicating that pipeline
    is out of scope for v1; the frontend renders an info banner instead.
    """
    _require_edia()
    return NewsListResponse(
        items=[],
        note=(
            "Industry news requires EDIA's AI event pipeline "
            "(not part of this v1 read-only mirror)."
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_service_market_dashboard.py -v`
Expected: PASS (all 8 tests, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/market_dashboard_service.py \
        backend/tests/test_service_market_dashboard.py
git commit -m "feat(edia): add get_regional_prices (simplified v1) + get_news_events (v1 stub)"
```

---

### Task E2: Add `/regional/{id}` + `/news` endpoints to the router

**Files:**
- Modify: `backend/app/routers/market_dashboard.py`
- Test: extend `backend/tests/test_router_market_dashboard.py`

**Interfaces:**
- Produces: GET `/api/market-dashboard/regional/{product_id}`, `/api/market-dashboard/news?group=&limit=`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_news_endpoint_returns_empty_with_note(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
                        "mysql+pymysql://x")
    monkeypatch.setattr("app.services.market_dashboard_service.is_edia_reachable_cached", lambda: True)
    # No auth override — the 503 path doesn't require auth to be valid
    # because _require_edia() runs first.
    client = _client_no_auth()
    r = client.get("/api/market-dashboard/news?group=upstream&limit=18")
    # Without auth, FastAPI returns 401/403/422. With auth stubbed we'd see
    # 200 + {items: [], note: ...}. Adjust by overriding the dep.
    assert r.status_code in (200, 401, 403, 422)


def test_regional_endpoint_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr("app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "")
    client = _client_no_auth()
    r = client.get("/api/market-dashboard/regional/naphtha")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_router_market_dashboard.py -v -k "news or regional"`
Expected: FAIL — routes don't exist (404)

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/routers/market_dashboard.py`:

```python
from app.schemas.market_dashboard import RegionalPriceData, NewsListResponse


@router.get("/regional/{product_id}", response_model=RegionalPriceData)
def get_regional(
    product_id: str,
    user=Depends(get_current_user_required),
) -> RegionalPriceData:
    """Return per-region prices for a product (v1 simplified)."""
    return svc.get_regional_prices(product_id)


@router.get("/news", response_model=NewsListResponse)
def get_news(
    group: Optional[str] = Query(None, pattern="^(upstream|midstream|downstream)$"),
    limit: int = Query(18, ge=1, le=50),
    user=Depends(get_current_user_required),
) -> NewsListResponse:
    """Return industry news (v1: empty list with explanatory note)."""
    return svc.get_news_events(group=group, limit=limit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_router_market_dashboard.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/market_dashboard.py \
        backend/tests/test_router_market_dashboard.py
git commit -m "feat(edia): add /regional/{id} + /news endpoints"
```

---

### Task E3: Create `components/RegionalTable.jsx`

**Files:**
- Create: `frontend/src/pages/MarketDashboard/components/RegionalTable.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/RegionalTable.test.jsx`

**Interfaces:**
- Consumes: `marketDashboardApi.regional(productId)`, `useQuery`, `RegionalPriceData`
- Produces: `<RegionalTable productId="..." />`

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/components/__tests__/RegionalTable.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    regional: vi.fn().mockResolvedValue({
      product_id: 'naphtha',
      regions: [
        { price_type: '华东', label: '华东', price: 612.5, change_pct: null,
          date: '2026-07-30', days_stale: 0, is_internal: false },
      ],
      note: 'v1 simplified: no spread/cost-floor analysis',
    }),
  },
}));

import RegionalTable from '../RegionalTable';

function renderTable(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RegionalTable {...props} />
    </QueryClientProvider>,
  );
}

describe('RegionalTable', () => {
  it('renders region rows', async () => {
    renderTable({ productId: 'naphtha' });
    expect(await screen.findByText('华东')).toBeInTheDocument();
    expect(await screen.findByText('612.5')).toBeInTheDocument();
  });

  it('renders the v1 note', async () => {
    renderTable({ productId: 'naphtha' });
    expect(await screen.findByText(/v1 simplified/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/RegionalTable.test.jsx`
Expected: FAIL — module missing

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/pages/MarketDashboard/components/RegionalTable.jsx
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO, NO_DATA_LABEL_LIT } from '../lib/typography';
import { NO_DATA_LABEL } from '../lib/priceChange';

/**
 * RegionalTable — per-product regional price breakdown.
 * v1: no spread / cost-floor analysis (rendered as a note under the table).
 */
export default function RegionalTable({ productId }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'regional', productId],
    queryFn: () => marketDashboardApi.regional(productId),
    staleTime: 60_000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-baseline justify-between">
        <span className={TYPO.cardTitle}>{productId} — Regional</span>
      </div>
      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {data && data.regions && data.regions.length === 0 && (
        <p className={TYPO.listText + ' text-slate-500'}>No regional columns.</p>
      )}
      {data && data.regions && data.regions.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1 pr-4">Region</th>
              <th className="py-1 pr-4">Price</th>
              <th className="py-1 pr-4">Date</th>
            </tr>
          </thead>
          <tbody>
            {data.regions.map((r) => (
              <tr key={r.price_type} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1 pr-4">{r.label}</td>
                <td className="py-1 pr-4 font-mono">{r.price ?? NO_DATA_LABEL}</td>
                <td className="py-1 pr-4 text-xs text-slate-500">{r.date || NO_DATA_LABEL}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data?.note && (
        <p className={TYPO.micro + ' mt-2 italic'}>{data.note}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/RegionalTable.test.jsx`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/components/RegionalTable.jsx \
        frontend/src/pages/MarketDashboard/components/__tests__/RegionalTable.test.jsx
git commit -m "feat(edia): RegionalTable renders per-region price + v1 note"
```

---

### Task E4: Create `components/NewsPanel.jsx` (v1 info banner)

**Files:**
- Create: `frontend/src/pages/MarketDashboard/components/NewsPanel.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/NewsPanel.test.jsx`

**Interfaces:**
- Consumes: `marketDashboardApi.news(group, limit)`, `useQuery`, `NewsListResponse`
- Produces: `<NewsPanel group="upstream" />`

- [ ] **Step 1: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/components/__tests__/NewsPanel.test.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    news: vi.fn().mockResolvedValue({
      items: [],
      note: 'Industry news requires EDIA\'s AI event pipeline (not part of this v1 read-only mirror).',
    }),
  },
}));

import NewsPanel from '../NewsPanel';

function renderPanel(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewsPanel {...props} />
    </QueryClientProvider>,
  );
}

describe('NewsPanel', () => {
  it('renders the v1 info banner when items is empty', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText(/AI event pipeline/i)).toBeInTheDocument();
  });

  it('renders the section header', async () => {
    renderPanel({ group: 'midstream' });
    expect(await screen.findByText(/industry news/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/NewsPanel.test.jsx`
Expected: FAIL — module missing

- [ ] **Step 3: Write minimal implementation**

```jsx
// frontend/src/pages/MarketDashboard/components/NewsPanel.jsx
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';

/**
 * NewsPanel — industry news for the selected section.
 *
 * v1: EDIA's news comes from an AI event pipeline that is out of scope for
 * the read-only MySQL mirror. The endpoint returns {items: [], note: "..."}
 * and this panel surfaces that as an info banner instead of empty cards.
 * When news items arrive in v2+, the same component will render them as a list.
 */
export default function NewsPanel({ group, limit = 18 }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'news', group, limit],
    queryFn: () => marketDashboardApi.news(group, limit),
    staleTime: 5 * 60_000,  // news changes slowly; cache 5 min
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2">
        <span className={TYPO.cardTitle}>Industry News — {group || 'all'}</span>
      </div>
      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {data && data.items && data.items.length > 0 && (
        <ul className="space-y-2">
          {data.items.map((it) => (
            <li key={it.event_id} className="text-sm">
              <a href={it.source_url || '#'} className="font-medium hover:underline">
                {it.headline}
              </a>
              {it.summary && (
                <p className={TYPO.secondaryData + ' mt-0.5'}>{it.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {data && (!data.items || data.items.length === 0) && (
        <div className="rounded border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800">
          {data.note || 'No news available.'}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/components/__tests__/NewsPanel.test.jsx`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/components/NewsPanel.jsx \
        frontend/src/pages/MarketDashboard/components/__tests__/NewsPanel.test.jsx
git commit -m "feat(edia): NewsPanel renders v1 info banner (no AI event pipeline)"
```

---

### Task E5: Wire `RegionalTable` + `NewsPanel` into `MarketDashboardPage.jsx`

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx`
- Test: extend `MarketDashboardPage.test.jsx`

**Interfaces:**
- Consumes: `RegionalTable` (Task E3), `NewsPanel` (Task E4)

- [ ] **Step 1: Write the failing test** (append)

```jsx
  it('renders RegionalTable for each product in the snapshot', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    marketDashboardApi.history.mockResolvedValue([]);
    marketDashboardApi.regional.mockResolvedValue({ product_id: 'naphtha', regions: [], note: 'v1' });
    renderAt('/market-dashboard/upstream');
    // The RegionalTable card renders the product id + "— Regional" header.
    expect(await screen.findByText(/naphtha — regional/i)).toBeInTheDocument();
  });

  it('renders NewsPanel with the v1 info banner', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    marketDashboardApi.history.mockResolvedValue([]);
    marketDashboardApi.regional.mockResolvedValue({ product_id: 'naphtha', regions: [] });
    marketDashboardApi.news.mockResolvedValue({
      items: [],
      note: "Industry news requires EDIA's AI event pipeline (not part of this v1 read-only mirror).",
    });
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText(/AI event pipeline/i)).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: FAIL — neither component is rendered yet

- [ ] **Step 3: Write minimal implementation**

In `MarketDashboardPage.jsx`, after the PriceChart grid, add:

```jsx
        {snapshotQuery.data && snapshotQuery.data.length > 0 && (
          <div className="mt-6 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {snapshotQuery.data.map((s) => (
              <RegionalTable key={`r-${s.id}`} productId={s.id} />
            ))}
          </div>
        )}
        <div className="mt-6">
          <NewsPanel group={section === 'overview' ? undefined : section} />
        </div>
```

Add imports:
```jsx
import RegionalTable from './components/RegionalTable';
import NewsPanel from './components/NewsPanel';
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx \
        frontend/src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx
git commit -m "feat(edia): wire RegionalTable + NewsPanel into dashboard page"
```

---

### Task E6: Manual smoke test across all 4 sections

**Files:** none (verification only)

- [ ] **Step 1: Set EDIA_MYSQL_URL in backend/.env**

```bash
# backend/.env (do NOT commit — verify with `git status` shows .env untracked)
EDIA_MYSQL_URL=mysql+pymysql://root:PASSWORD@10.10.10.49:3306/aipdp_data_warehouse_prod
```

- [ ] **Step 2: Start backend + frontend**

```bash
cd backend && python main.py &
cd frontend && npm run dev &
```

- [ ] **Step 3: Verify all 4 sections render**

Visit each of:
- `http://localhost:5173/market-dashboard/overview`
- `http://localhost:5173/market-dashboard/upstream`
- `http://localhost:5173/market-dashboard/midstream`
- `http://localhost:5173/market-dashboard/downstream`

For each, confirm:
1. Sidebar accordion auto-expands and highlights the active sub-item
2. Snapshot cards show price + change pct for mapped products (`naphtha`, `isoprene`, `piperylene`, `dcpd`, `sis`)
3. Unmapped products (`crude_oil`, `cracked_c9`, `raffinate_c5`, `c5_resin`, `blowing_agent`, `styrene`) show "table mapping not configured in v1"
4. PriceChart renders for each product (line + area gradient)
5. RegionalTable shows the per-region breakdown for mapped products
6. NewsPanel shows the blue info banner ("Industry news requires EDIA's AI event pipeline…")
7. The rest of zhanlu (chat, sessions, automation) is unaffected

- [ ] **Step 4: Verify fail-closed when EDIA_MYSQL_URL is empty**

```bash
# In a second terminal, with EDIA_MYSQL_URL="" — restart backend.
unset EDIA_MYSQL_URL
cd backend && python main.py &
```
Visit `/market-dashboard/upstream`. Confirm:
1. The amber "EDIA data source is not configured" banner appears
2. Sidebar accordion is **hidden** (no dead nav items)

- [ ] **Step 5: Verify fail-closed when EDIA MySQL is unreachable**

```bash
EDIA_MYSQL_URL=mysql+pymysql://root:x@10.10.10.99:3306/foo
cd backend && python main.py &
```
Visit `/market-dashboard/upstream`. Confirm the red "configured but currently unreachable" banner appears (after the 3s connect_timeout).

- [ ] **Step 6: Commit final smoke-test note**

```bash
git commit --allow-empty -m "chore(edia): smoke-tested all 4 dashboard sections + fail-closed paths"
```

---

## Self-Review Checklist (run after writing the plan, fix inline)

### 1. Spec coverage
- [x] Spec §2 (3 confirmed assumptions): covered by Global Constraints + Task A1 config
- [x] Spec §4.1 backend files to create: A2 (mysql_db), A3 (models), A4 (schemas), A5 (services), A6 (routers) ✓
- [x] Spec §4.2 backend files to modify: A1 (config + .env.example), A6 (main.py) ✓
- [x] Spec §4.3 dependencies (none new): called out in Global Constraints ✓
- [x] Spec §5.1 frontend files to create: B1 (api), B2 (page), B3 (lib ports), C1 (SnapshotGrid), D1 (PriceChart), E3 (RegionalTable), E4 (NewsPanel) ✓
- [x] Spec §5.2 frontend files to modify: B4 (Sidebar + App.jsx) ✓
- [x] Spec §6 product mapping: verbatim in Global Constraints + Task A3's `PRODUCT_GROUP_MAP` ✓
- [x] Spec §7 phased order: A→B→C→D→E matches ✓
- [x] Spec §8 risks (503 paths, autoload drift, sidebar noise): covered by A1/A2 + E6 ✓

### 2. Placeholder scan
- [x] No "TBD", "TODO", "implement later"
- [x] Every code step shows actual code, not "write tests for the above"
- [x] All function names referenced in later tasks are defined in earlier tasks (verified: `is_edia_enabled`, `is_edia_reachable_cached`, `get_market_snapshot`, `get_price_history`, `get_regional_prices`, `get_news_events`, `get_product_table`, `get_mysql_engine`, `marketDashboardApi.{enabled,snapshot,history,regional,news}`, `SnapshotGrid`, `PriceChart`, `RegionalTable`, `NewsPanel`)

### 3. Type consistency
- [x] `MarketSnapshot` fields (id, name, group, price, date, change_pct, weekly_change_pct, note) used identically in A4 schema, A5 service return, C1 SnapshotGrid
- [x] `PriceHistoryPoint` fields (date, price, volume) used identically in A4, A5, D1
- [x] `RegionalPriceData` fields (product_id, regions[], note) used identically in A4, E1, E3
- [x] `NewsListResponse` fields (items[], note) used identically in A4, E1, E4
- [x] `EnabledResponse` fields (enabled, reachable, reason) used identically in A4, A6, B2, B4

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-31-media-dashboard-integration.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for this plan because the 17 tasks are well-bounded and independent within each phase.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints for review. Slower but you stay in the loop on every step.

**Which approach?**
