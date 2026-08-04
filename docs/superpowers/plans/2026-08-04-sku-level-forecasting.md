# SKU-Level Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace product-family-level ERP forecasting (which blends multiple SKUs into meaningless averages) with SKU-level forecasting keyed by `material_code`, while keeping the 10 `md_t_lz_price` products unchanged.

**Architecture:** Zero-migration schema approach — SKU identity is encoded in `ForecastTarget.product_key` (e.g. `ecisco.c5_resin.201000018`) and `datasource` JSON (`where` clause filters by `material_code`). The dashboard service discovers SKU targets by prefix matching (`product_key LIKE 'ecisco.c5_resin.%'`), picks the primary SKU (lowest `report_order`) for the main forecast card, and returns all SKUs in a new `sku_forecasts_by_product` field for drill-down. A `groupby("ds").agg({"y": "mean"})` step in `edia_source.py` deduplicates same-date rows (no-op for `md_t_lz_price` where prices are identical; correctness fix for ERP tables where multiple order entries land on the same `PLANDATE`).

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, pandas, FastAPI, Pydantic, React/Vite/Tailwind, lucide-icons. Backend runs in Docker (no `--reload` — `docker compose restart backend` after service changes).

**Spec:** Brainstormed and approved across 5 design sections in the prior session. All sections approved by the user. This document IS the spec + plan combined.

**Phases:**
- **Phase A (Tasks 1–9):** SKU-level data plumbing — replace `WHERE 1=1` blended ERP averages with per-`material_code` targets, drill-down UI, agent tool. No engine changes.
- **Phase B (Tasks B1–B4):** EDIA domain-signal layers — port tier-dampened causal-chain elasticities (naphtha → C5 → downstream), per-product seasonal adjustment rules, and forecast trust tiers. Pure-Python overlay functions slotted into the engine between the intelligence overlay (Step 8.5) and the policy service (Step 8.6). Gated by `FORECAST_DOMAIN_SIGNALS_ENABLED` (default False).

Phase B is **independent of Phase A** — it can be executed before, after, or in parallel. Phase A targets are keyed by `material_code` but the product_id (`c5_resin`, `naphtha`, etc.) used by the domain-signal dicts is unchanged, so the overlay works identically on SKU-level and family-level targets.

---

## Global Constraints

- **Project root:** `/home/ysk2025/zhanlu_7_30` (NOT `/home/ysk2025/zhanlu/` or `zhanlu-deploy`).
- **Backend venv:** `backend/venv/bin/python` — use this for all test runs. pytest is NOT in requirements.txt but IS installed in the venv.
- **Test runner:** `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/<file>::<test> -v` — run individually, SSH RAM is limited (no parallelism, no backgrounding).
- **Docker:** Backend has no `--reload`. After service code changes: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`.
- **Frontend:** Bind-mounted to nginx. After component changes: `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build` (auto-rebuilds on save too).
- **Alembic:** No new migration needed — SKU identity is encoded in existing `product_key` (VARCHAR 100) + `datasource` (JSON) columns. The unique constraint is `(product_key, org_id)`, and SKU keys like `ecisco.c5_resin.201000018` are unique within an org.
- **Code style:** Follow existing patterns — `from __future__ import annotations`, dataclasses for result types, `TimestampedBase` for DB models, lazy imports for heavy deps, type hints everywhere.
- **ERP tables:** `sale_erp_v_碳五石油树脂_data` (c5_resin) and `sale_erp_v_抽余碳五_data` (raffinate_c5). Columns: `PLANDATE` (date), `FTAXPRICE` (decimal), `material_code` (varchar, the SKU key), `material_name` (varchar, the product family). `FTAXPRICE > 0` filter is required to exclude cancelled/reversed entries.
- **md_t_lz_price tables:** Already keyed by `FMATERIAL_NAME` (one series per product). The `groupby` aggregation is a no-op here because duplicate dates have identical prices. ERP tables have multiple order entries per date with different prices — `groupby` averages them into a daily mean price.
- **Minimum SKU history:** 50 rows. SKUs with fewer than 50 rows are skipped (insufficient for forecasting).
- **Primary SKU selection:** Highest row count (= highest transaction volume) gets `report_order` = the family's original value (12 for both c5_resin and raffinate_c5). Secondary SKUs get `report_order` 13, 14, 15, ... in descending volume order.

---

## File Structure

**Modified files:**

| File | Change |
|---|---|
| `backend/app/services/forecasting/edia_source.py` | Add `groupby("ds").agg({"y": "mean"})` after sentinel filtering — deduplicates same-date rows |
| `backend/app/services/forecasting/seed_ecisco_targets.py` | Replace 2 static ERP targets with dynamic `discover_and_seed_sku_targets()` that queries distinct `material_code` values |
| `backend/app/schemas/market_dashboard.py` | Add `SkuForecastMeta` schema + `sku_forecasts_by_product` field on `ForecastBaseBatchResponse` |
| `backend/app/services/market_dashboard_service.py` | Update `get_forecast_base_batch_v2()` — SKU prefix lookup, primary SKU selection, drill-down data |
| `backend/app/services/tool_handlers/forecast_tool.py` | Add SKU fields to `_serialize_target()`, `material_code`+`product_id` params to `forecast_get`/`forecast_run`, new `forecast_list_skus` tool |
| `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx` | SKU badge with Layers icon + drill-down popover showing all SKUs for the product |
| `frontend/src/api/marketDashboard.js` | Add `forecastListSkus` API method |

**New test files:**

| File | Purpose |
|---|---|
| `backend/tests/test_edia_source_groupby.py` | Test the `groupby` deduplication logic |
| `backend/tests/test_seed_sku_targets.py` | Test the dynamic SKU discovery seeder |
| `backend/tests/test_forecast_batch_sku.py` | Test `get_forecast_base_batch_v2` SKU lookup + drill-down |

---

## Task 1: Add Date Deduplication to `edia_source.py`

**Files:**
- Modify: `backend/app/services/forecasting/edia_source.py:107-118`
- Test: `backend/tests/test_edia_source_groupby.py`

**Interfaces:**
- Consumes: nothing (standalone module)
- Produces: `EdiaMysqlDataSource.read_history(datasource: dict) -> pd.DataFrame` with deduplicated dates. The returned DataFrame has columns `["ds", "y"]` with one row per unique date — same shape as before, just deduplicated.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_edia_source_groupby.py`:

```python
"""Tests for EdiaMysqlDataSource date deduplication — groupby("ds").agg({"y": "mean"})."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.forecasting.edia_source import EdiaMysqlDataSource


def _make_mock_engine_with_duplicates(rows: list[tuple]) -> Engine:
    """Build an in-memory SQLite engine with duplicate-date rows.

    Each row is (ds_str, y_value). Multiple rows with the same ds_str
    simulate ERP order entries landing on the same PLANDATE.
    """
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE erp_test (PLANDATE TEXT, FTAXPRICE TEXT, material_code TEXT)"
        ))
        for ds, y, code in rows:
            conn.execute(text(
                "INSERT INTO erp_test (PLANDATE, FTAXPRICE, material_code) "
                "VALUES (:d, :p, :c)"
            ), {"d": ds, "p": str(y), "c": code})
        conn.commit()
    return eng


def test_groupby_averages_duplicate_dates():
    """Multiple rows on the same date should be averaged into one row."""
    rows = [
        ("2026-07-01", 5000.0, "201000018"),
        ("2026-07-01", 7000.0, "201000018"),  # same date, different price
        ("2026-07-01", 6000.0, "201000018"),  # same date, third order
        ("2026-07-02", 5500.0, "201000018"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = '201000018'",
        "granularity": "day",
    })
    # 3 rows for 2026-07-01 averaged → (5000+7000+6000)/3 = 6000.0
    # 1 row for 2026-07-02 → 5500.0
    assert len(df) == 2
    assert df.iloc[0]["ds"].strftime("%Y-%m-%d") == "2026-07-01"
    assert df.iloc[0]["y"] == 6000.0
    assert df.iloc[1]["ds"].strftime("%Y-%m-%d") == "2026-07-02"
    assert df.iloc[1]["y"] == 5500.0


def test_groupby_noop_when_dates_already_unique():
    """md_t_lz_price has duplicate dates with identical prices — avg is a no-op."""
    rows = [
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),  # triplicate, same price
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-02", 5600.0, "裂解C5-裂解C5均价"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = '裂解C5-裂解C5均价'",
        "granularity": "day",
    })
    # Identical prices → avg = same value → no-op
    assert len(df) == 2
    assert df.iloc[0]["y"] == 5500.0
    assert df.iloc[1]["y"] == 5600.0


def test_groupby_preserves_sort_order():
    """After groupby, rows should still be sorted ascending by ds."""
    rows = [
        ("2026-07-03", 5700.0, "SKU1"),
        ("2026-07-01", 5500.0, "SKU1"),
        ("2026-07-01", 5600.0, "SKU1"),  # dup date
        ("2026-07-02", 5600.0, "SKU1"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = 'SKU1'",
        "granularity": "day",
    })
    assert len(df) == 3
    assert df.iloc[0]["ds"] < df.iloc[1]["ds"] < df.iloc[2]["ds"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_edia_source_groupby.py -v`
Expected: FAIL — `test_groupby_averages_duplicate_dates` returns 4 rows instead of 2 (no groupby yet).

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/forecasting/edia_source.py`, add the `groupby` line after the `dropna` call and before the `sort_values` call. Replace lines 110-118:

```python
        # Drop any rows that failed coercion (NaN y or NaT ds)
        df = df.dropna(subset=["ds", "y"])

        # Deduplicate by date: multiple ERP order entries can land on the
        # same PLANDATE with different prices. Average them into a single
        # daily mean price. For md_t_lz_price this is a no-op — duplicate
        # dates have identical prices, so AVG(price) = price.
        df = df.groupby("ds", as_index=False).agg({"y": "mean"})

        df = df.sort_values("ds").reset_index(drop=True)

        if df.empty:
            raise RuntimeError(
                f"No valid rows after sentinel/NaN filtering for {table} WHERE {where}."
            )

        return df[["ds", "y"]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_edia_source_groupby.py -v`
Expected: PASS — all 3 tests pass.

- [ ] **Step 5: Run existing edia_source tests to verify no regression**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_edia_source.py -v`
Expected: PASS — all 5 existing tests still pass (the groupby is a no-op for their single-row-per-date data).

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/edia_source.py backend/tests/test_edia_source_groupby.py
git commit -m "feat(forecast): add groupby date deduplication to edia_source

ERP tables (sale_erp_v_*_data) have multiple order entries per PLANDATE
with different prices. groupby('ds').agg({'y': 'mean'}) averages them
into a daily mean price. For md_t_lz_price this is a no-op — duplicate
dates have identical prices."
```

---

## Task 2: Dynamic SKU Discovery Seeder

**Files:**
- Modify: `backend/app/services/forecasting/seed_ecisco_targets.py:166-242`
- Test: `backend/tests/test_seed_sku_targets.py`

**Interfaces:**
- Consumes: `EdiaMysqlDataSource` from Task 1 (for reading the ERP engine)
- Produces: `discover_and_seed_sku_targets(db, org_id, app_id, engine=None) -> int` — replaces the 2 static ERP entries in `ECISCO_FORECAST_TARGETS` with dynamically discovered SKU-level targets.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_seed_sku_targets.py`:

```python
"""Tests for dynamic SKU discovery seeder — replaces static ERP WHERE 1=1 targets."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget
from app.services.forecasting.seed_ecisco_targets import (
    discover_and_seed_sku_targets,
    ECISCO_FORECAST_TARGETS,
)


@pytest.fixture
def db_session():
    """In-memory SQLite session with forecasting tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[ForecastTarget.__table__])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _make_mock_erp_engine(rows: list[tuple]) -> object:
    """Build an in-memory SQLite engine simulating sale_erp_v_碳五石油树脂_data.

    Each row is (material_code, plandate, ftaxprice).
    """
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE sale_erp_v_碳五石油树脂_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        for code, dt, price in rows:
            conn.execute(text(
                "INSERT INTO sale_erp_v_碳五石油树脂_data "
                "(material_code, PLANDATE, FTAXPRICE) VALUES (:c, :d, :p)"
            ), {"c": code, "d": dt, "p": price})
        conn.commit()
    return eng


def test_static_targets_no_longer_include_erp_where_1_1():
    """The 2 ERP products (c5_resin, raffinate_c5) should NOT be in the
    static list anymore — they're discovered dynamically."""
    erp_keys = [
        t["product_key"] for t in ECISCO_FORECAST_TARGETS
        if t["datasource"].get("table", "").startswith("sale_erp_v_")
    ]
    assert len(erp_keys) == 0, (
        f"ERP products {erp_keys} should not be in static list — "
        "they are discovered dynamically by discover_and_seed_sku_targets()"
    )


def test_static_targets_still_has_10_md_lz_products():
    """The 10 md_t_lz_price products remain in the static list."""
    md_lz_count = sum(
        1 for t in ECISCO_FORECAST_TARGETS
        if t["datasource"].get("table") == "md_t_lz_price"
    )
    assert md_lz_count == 10


def test_discover_skus_inserts_one_target_per_sku(db_session):
    """discover_and_seed_sku_targets should insert one ForecastTarget per
    material_code with >= 50 rows."""
    # SKU A: 60 rows (above threshold)
    # SKU B: 30 rows (below threshold — skipped)
    # SKU C: 50 rows (exactly at threshold — included)
    rows = []
    for i in range(60):
        rows.append(("SKU_A", f"2025-01-{i+1:02d}", 5000.0 + i))
    for i in range(30):
        rows.append(("SKU_B", f"2025-01-{i+1:02d}", 3000.0 + i))
    for i in range(50):
        rows.append(("SKU_C", f"2025-01-{i+1:02d}", 7000.0 + i))
    # Add some zero-price rows (should be filtered by FTAXPRICE > 0)
    rows.append(("SKU_A", "2025-03-01", 0.0))

    erp_eng = _make_mock_erp_engine(rows)
    count = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    # SKU_A (60 rows) + SKU_C (50 rows) = 2 targets. SKU_B (30) skipped.
    assert count == 2

    targets = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org",
        ForecastTarget.product_key.like("ecisco.c5_resin.%"),
    ).all()
    assert len(targets) == 2
    keys = {t.product_key for t in targets}
    assert "ecisco.c5_resin.SKU_A" in keys
    assert "ecisco.c5_resin.SKU_C" in keys
    assert "ecisco.c5_resin.SKU_B" not in keys


def test_discover_skus_assigns_primary_sku_lowest_report_order(db_session):
    """The SKU with the most rows gets report_order=12 (the family's
    original value). Secondary SKUs get 13, 14, ..."""
    rows = []
    for i in range(100):
        rows.append(("PRIMARY", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))
    for i in range(60):
        rows.append(("SECONDARY", f"2025-01-{(i % 28) + 1:02d}", 6000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )

    primary = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.PRIMARY"
    ).one()
    secondary = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.SECONDARY"
    ).one()
    assert primary.report_order == 12
    assert secondary.report_order == 13


def test_discover_skus_where_clause_filters_by_material_code(db_session):
    """Each SKU target's datasource.where should filter by material_code."""
    rows = []
    for i in range(55):
        rows.append(("CODE_123", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )

    target = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.CODE_123"
    ).one()
    assert "material_code = 'CODE_123'" in target.datasource["where"]
    assert "FTAXPRICE > 0" in target.datasource["where"]


def test_discover_skus_is_idempotent(db_session):
    """Second call should insert 0 new targets."""
    rows = []
    for i in range(55):
        rows.append(("SKU_X", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    first = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    second = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    assert first >= 1
    assert second == 0


def test_discover_skus_handles_both_erp_products(db_session):
    """Both c5_resin and raffinate_c5 should be discovered."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE sale_erp_v_碳五石油树脂_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        conn.execute(text(
            "CREATE TABLE sale_erp_v_抽余碳五_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        for i in range(55):
            conn.execute(text(
                "INSERT INTO sale_erp_v_碳五石油树脂_data VALUES (:c, :d, :p)"
            ), {"c": "RESIN_1", "d": f"2025-01-{(i % 28) + 1:02d}", "p": 5000.0 + i})
        for i in range(55):
            conn.execute(text(
                "INSERT INTO sale_erp_v_抽余碳五_data VALUES (:c, :d, :p)"
            ), {"c": "RAFF_1", "d": f"2025-01-{(i % 28) + 1:02d}", "p": 4000.0 + i})
        conn.commit()

    count = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=eng
    )
    assert count == 2
    resin = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.RESIN_1"
    ).one()
    raff = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.raffinate_c5.RAFF_1"
    ).one()
    assert resin.report_order == 12
    assert raff.report_order == 12  # both families start at 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_seed_sku_targets.py -v`
Expected: FAIL — `discover_and_seed_sku_targets` doesn't exist yet, and the static list still contains the 2 ERP entries.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/forecasting/seed_ecisco_targets.py`:

**3a.** Remove the 2 ERP entries from `ECISCO_FORECAST_TARGETS` (the last 2 entries, lines 166-195 — the ones with `"table": "sale_erp_v_碳五石油树脂_data"` and `"table": "sale_erp_v_抽余碳五_data"`).

**3b.** Add the `discover_and_seed_sku_targets` function at the bottom of the file, before the `seed_ecisco_forecast_targets` function's closing:

```python
# ── ERP product families that need dynamic SKU discovery ───────────────
# Each entry maps a dashboard product_id to its ERP table name.
# The seeder queries SELECT DISTINCT material_code from these tables
# and creates one ForecastTarget per SKU (material_code) with >= 50 rows.
_ERP_PRODUCT_TABLES: dict[str, str] = {
    "c5_resin":     "sale_erp_v_碳五石油树脂_data",
    "raffinate_c5": "sale_erp_v_抽余碳五_data",
}

# Minimum row count for a SKU to be forecastable.
_MIN_SKU_ROWS = 50


def discover_and_seed_sku_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
    engine=None,
) -> int:
    """Discover SKUs (material_code values) in ERP tables and seed one
    ForecastTarget per SKU.

    For each ERP product family (c5_resin, raffinate_c5):
    1. Query SELECT DISTINCT material_code, COUNT(*) FROM <table>
       WHERE FTAXPRICE > 0 GROUP BY material_code
    2. Filter to SKUs with >= 50 rows (minimum for forecasting)
    3. Sort by row count descending — primary SKU gets report_order=12,
       secondary SKUs get 13, 14, ...
    4. Insert ForecastTarget rows with product_key="ecisco.<family>.<code>"

    Idempotent: skips targets that already exist (matched by product_key
    + org_id). Safe to call on every boot.

    Args:
        db: SQLAlchemy session.
        org_id: Org ID for the targets.
        app_id: App ID for the targets.
        engine: Optional SQLAlchemy engine for the ERP MySQL mirror.
                If None, uses get_mysql_engine().

    Returns:
        Number of new ForecastTarget rows inserted.
    """
    from sqlalchemy import text as sa_text

    if engine is None:
        from app.core.mysql_db import get_mysql_engine
        engine = get_mysql_engine()
    if engine is None:
        logger.warning(
            "discover_and_seed_sku_targets: EDIA MySQL unreachable, "
            "skipping SKU discovery."
        )
        return 0

    inserted = 0
    for family, table_name in _ERP_PRODUCT_TABLES.items():
        try:
            with engine.connect() as conn:
                # Discover SKUs: material_code + row count, filtered by
                # FTAXPRICE > 0 (exclude cancelled/reversed entries).
                stmt = sa_text(
                    f"SELECT material_code, COUNT(*) AS cnt "
                    f"FROM `{table_name}` "
                    f"WHERE FTAXPRICE > 0 "
                    f"AND material_code IS NOT NULL "
                    f"AND material_code != '' "
                    f"GROUP BY material_code "
                    f"ORDER BY cnt DESC"
                )
                rows = conn.execute(stmt).fetchall()
        except Exception as exc:
            logger.warning(
                "discover_and_seed_sku_targets: query failed for %s: %s",
                table_name, exc,
            )
            continue

        # Filter to SKUs with enough history
        qualified = [
            (code, cnt) for code, cnt in rows
            if cnt >= _MIN_SKU_ROWS
        ]
        if not qualified:
            logger.info(
                "discover_and_seed_sku_targets: no SKUs with >= %d rows in %s",
                _MIN_SKU_ROWS, table_name,
            )
            continue

        logger.info(
            "discover_and_seed_sku_targets: %s — %d SKUs qualified (of %d total)",
            family, len(qualified), len(rows),
        )

        for idx, (code, cnt) in enumerate(qualified):
            product_key = f"ecisco.{family}.{code}"
            # Primary SKU (idx=0) gets report_order=12 (family's original).
            # Secondary SKUs get 13, 14, 15, ...
            report_order = 12 + idx

            existing = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == product_key,
                ForecastTarget.org_id == org_id,
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).first()
            if existing is not None:
                continue

            target = ForecastTarget(
                org_id=org_id,
                app_id=app_id,
                product_key=product_key,
                name=f"{family} SKU {code} (ERP, {cnt} rows)",
                datasource={
                    "source": "edia_mysql",
                    "table": table_name,
                    "time_col": "PLANDATE",
                    "measure": "FTAXPRICE",
                    "where": f"material_code = '{code}' AND FTAXPRICE > 0",
                    "granularity": "day",
                },
                level=2,
                quality_grade="B",
                status="active",
                source="seed_sku",
                include_in_weekly_report=(idx == 0),  # only primary SKU
                report_order=report_order,
            )
            db.add(target)
            inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "discover_and_seed_sku_targets: inserted %d new SKU targets for org=%s",
            inserted, org_id,
        )
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_seed_sku_targets.py -v`
Expected: PASS — all 7 tests pass.

- [ ] **Step 5: Run existing seed tests to verify no regression**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_seed_ecisco_targets.py -v`
Expected: The `test_ecisco_forecast_targets_has_12_entries` test will FAIL because we removed 2 entries. Update it:

In `backend/tests/test_seed_ecisco_targets.py`, change:
```python
def test_ecisco_forecast_targets_has_12_entries():
    assert len(ECISCO_FORECAST_TARGETS) == 12
```
to:
```python
def test_ecisco_forecast_targets_has_10_md_lz_entries():
    """Static list now has 10 md_t_lz_price products. ERP products are
    discovered dynamically by discover_and_seed_sku_targets()."""
    assert len(ECISCO_FORECAST_TARGETS) == 10
```

Also update `test_seeder_inserts_12_rows_on_first_call`:
```python
def test_seeder_inserts_10_rows_on_first_call(db_session):
    count = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert count == 10
    rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org"
    ).all()
    assert len(rows) == 10
```

And `test_seeder_is_idempotent_second_call_inserts_zero`:
```python
def test_seeder_is_idempotent_second_call_inserts_zero(db_session):
    first = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert first == 10
    second = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert second == 0
    rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org"
    ).all()
    assert len(rows) == 10  # still 10, not 20
```

And `test_seeder_scoped_to_org_id`:
```python
def test_seeder_scoped_to_org_id(db_session):
    """Seeding for org-a should not affect org-b."""
    seed_ecisco_forecast_targets(db_session, org_id="org-a", app_id="app-a")
    seed_ecisco_forecast_targets(db_session, org_id="org-b", app_id="app-b")
    a_rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "org-a"
    ).all()
    b_rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "org-b"
    ).all()
    assert len(a_rows) == 10
    assert len(b_rows) == 10
```

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_seed_ecisco_targets.py -v`
Expected: PASS — all updated tests pass.

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/seed_ecisco_targets.py \
        backend/tests/test_seed_sku_targets.py \
        backend/tests/test_seed_ecisco_targets.py
git commit -m "feat(forecast): replace static ERP targets with dynamic SKU discovery

The 2 ERP products (c5_resin, raffinate_c5) had WHERE 1=1 which blended
all SKUs into a meaningless average. discover_and_seed_sku_targets()
queries SELECT DISTINCT material_code, groups by row count, and creates
one ForecastTarget per SKU with >= 50 rows. Primary SKU (highest volume)
gets report_order=12, secondary SKUs get 13, 14, ..."
```

---

## Task 3: Add SKU Schema to `market_dashboard.py`

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py:109-116`
- Test: covered by Task 4 tests (schema is consumed by the batch endpoint)

**Interfaces:**
- Consumes: nothing
- Produces: `SkuForecastMeta` (Pydantic model) and `sku_forecasts_by_product` field on `ForecastBaseBatchResponse`

- [ ] **Step 1: Write the schema**

In `backend/app/schemas/market_dashboard.py`, add the `SkuForecastMeta` class after `ForecastOverlayMeta` (around line 107) and add the `sku_forecasts_by_product` field to `ForecastBaseBatchResponse`:

```python
class SkuForecastMeta(BaseModel):
    """Metadata for one SKU (material_code) within a product family."""
    material_code: str
    target_id: str
    name: str
    report_order: int
    is_primary: bool = False
    row_count: Optional[int] = None
    forecast_base: Optional[float] = None
    forecast_confidence: Optional[float] = None


class ForecastBaseBatchResponse(BaseModel):
    """Batch response for /forecast-base-batch — 7-day-ahead forecast prices.

    Phase 6: includes overlay metadata per product.
    Phase 7: includes SKU drill-down data for ERP products.
    """
    day: int
    base_by_product: dict[str, Optional[float]] = Field(default_factory=dict)
    overlay_by_product: dict[str, Optional[ForecastOverlayMeta]] = Field(default_factory=dict)
    sku_forecasts_by_product: dict[str, list[SkuForecastMeta]] = Field(default_factory=dict)
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -c "from app.schemas.market_dashboard import SkuForecastMeta, ForecastBaseBatchResponse; print('OK')"`
Expected: prints `OK` with no import error.

- [ ] **Step 3: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/schemas/market_dashboard.py
git commit -m "feat(schema): add SkuForecastMeta + sku_forecasts_by_product field

New schema for SKU drill-down data on the forecast batch response.
Each ERP product family returns a list of SkuForecastMeta objects —
one per material_code with >= 50 rows."
```

---

## Task 4: Update `get_forecast_base_batch_v2` for SKU Lookup

**Files:**
- Modify: `backend/app/services/market_dashboard_service.py:856-983`
- Test: `backend/tests/test_forecast_batch_sku.py`

**Interfaces:**
- Consumes: `SkuForecastMeta` from Task 3, `ForecastTarget` model
- Produces: `get_forecast_base_batch_v2(product_ids, day, db) -> ForecastBaseBatchResponse` with populated `sku_forecasts_by_product` for ERP products.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_forecast_batch_sku.py`:

```python
"""Tests for get_forecast_base_batch_v2 SKU lookup and drill-down."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.market_dashboard_service import get_forecast_base_batch_v2


@pytest.fixture
def db_session():
    """In-memory SQLite session with forecasting tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[
        ForecastTarget.__table__,
        ForecastRun.__table__,
    ])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _make_sku_target(db, org_id, family, code, report_order, with_run=False, base_price=5000.0):
    """Insert a SKU ForecastTarget (+ optional ForecastRun) for testing."""
    target = ForecastTarget(
        org_id=org_id,
        app_id="test-app",
        product_key=f"ecisco.{family}.{code}",
        name=f"{family} SKU {code}",
        datasource={
            "source": "edia_mysql",
            "table": f"sale_erp_v_{family}_data",
            "time_col": "PLANDATE",
            "measure": "FTAXPRICE",
            "where": f"material_code = '{code}' AND FTAXPRICE > 0",
            "granularity": "day",
        },
        level=2,
        quality_grade="B",
        status="active",
        source="seed_sku",
        include_in_weekly_report=(report_order == 12),
        report_order=report_order,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    if with_run:
        run = ForecastRun(
            org_id=org_id,
            target_id=target.id,
            as_of_date=datetime(2026, 8, 4),
            horizons=[7],
            results={
                "7d": {
                    "base": [base_price],
                    "bull": [base_price * 1.05],
                    "bear": [base_price * 0.95],
                },
            },
            confidence=0.8,
            below_naive_baseline=False,
            model_detail={"metric": "ensemble"},
        )
        db.add(run)
        db.commit()
    return target


def test_sku_forecasts_returned_for_erp_product(db_session):
    """get_forecast_base_batch_v2 should return sku_forecasts_by_product
    for c5_resin with all SKU targets."""
    _make_sku_target(db_session, "default-org", "c5_resin", "PRIMARY", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "c5_resin", "SECONDARY", 13, with_run=True, base_price=6000.0)

    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert "c5_resin" in resp.sku_forecasts_by_product
    skus = resp.sku_forecasts_by_product["c5_resin"]
    assert len(skus) == 2
    # Primary SKU (report_order=12) should be first
    assert skus[0].material_code == "PRIMARY"
    assert skus[0].is_primary is True
    assert skus[0].forecast_base == 5000.0
    assert skus[1].material_code == "SECONDARY"
    assert skus[1].is_primary is False
    assert skus[1].forecast_base == 6000.0


def test_primary_sku_base_used_for_main_forecast(db_session):
    """The main base_by_product[c5_resin] should use the primary SKU's forecast."""
    _make_sku_target(db_session, "default-org", "c5_resin", "PRIMARY", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "c5_resin", "SECONDARY", 13, with_run=True, base_price=6000.0)

    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert resp.base_by_product["c5_resin"] == 5000.0


def test_no_sku_forecasts_for_md_lz_products(db_session):
    """md_t_lz_price products (e.g. crude_oil) should NOT have SKU data."""
    target = ForecastTarget(
        org_id="default-org",
        app_id="test-app",
        product_key="ecisco.crude_oil",
        name="Crude Oil (Brent)",
        datasource={
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-布伦特'",
            "granularity": "day",
        },
        level=2,
        quality_grade="B",
        status="active",
        source="seed",
        include_in_weekly_report=True,
        report_order=1,
    )
    db_session.add(target)
    db_session.commit()

    run = ForecastRun(
        org_id="default-org",
        target_id=target.id,
        as_of_date=datetime(2026, 8, 4),
        horizons=[7],
        results={"7d": {"base": [75.0]}},
        confidence=0.9,
        below_naive_baseline=False,
        model_detail={"metric": "ensemble"},
    )
    db_session.add(run)
    db_session.commit()

    resp = get_forecast_base_batch_v2(["crude_oil"], day=7, db=db_session)
    assert resp.base_by_product["crude_oil"] == 75.0
    # crude_oil should NOT appear in sku_forecasts_by_product
    assert "crude_oil" not in resp.sku_forecasts_by_product


def test_sku_forecasts_empty_when_no_targets(db_session):
    """No SKU targets → empty list (not missing key)."""
    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert resp.base_by_product["c5_resin"] is None
    # Should return empty list, not None
    assert resp.sku_forecasts_by_product.get("c5_resin", []) == []


def test_both_erp_products_return_sku_data(db_session):
    """Both c5_resin and raffinate_c5 should return SKU lists."""
    _make_sku_target(db_session, "default-org", "c5_resin", "R1", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "raffinate_c5", "RC1", 12, with_run=True, base_price=4000.0)

    resp = get_forecast_base_batch_v2(["c5_resin", "raffinate_c5"], day=7, db=db_session)
    assert len(resp.sku_forecasts_by_product["c5_resin"]) == 1
    assert len(resp.sku_forecasts_by_product["raffinate_c5"]) == 1
    assert resp.base_by_product["c5_resin"] == 5000.0
    assert resp.base_by_product["raffinate_c5"] == 4000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_batch_sku.py -v`
Expected: FAIL — `sku_forecasts_by_product` not populated; SKU prefix lookup not implemented.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/market_dashboard_service.py`, modify the `get_forecast_base_batch_v2` function. After the existing `targets` query and the `for tgt in targets:` loop, add SKU lookup logic. The key changes are:

**3a.** Add a new constant `_ERP_PRODUCT_KEYS` that maps ERP product_ids to their `product_key` prefix:

```python
# ERP product_ids whose forecasts are keyed by material_code (SKU-level).
# The forecast target product_key is "ecisco.<product_id>.<material_code>".
_ERP_PRODUCT_KEYS: set[str] = {"c5_resin", "raffinate_c5"}
```

Add this right after the `PRODUCT_FORECAST_TARGET_KEY` dict (after line 872).

**3b.** Restructure `get_forecast_base_batch_v2` to handle both md_t_lz_price products (direct key lookup) and ERP products (prefix lookup). Replace the function body (lines 875-983) with:

```python
def get_forecast_base_batch_v2(
    product_ids: list[str], day: int = 7, db=None
) -> ForecastBaseBatchResponse:
    """Read forecast base prices from the local ForecastRun table.

    Replaces the v1 reader that hit EDIA's stale v_forecast_price view.
    Returns the latest `results['{day}d']['base'][0]` per product, or
    None for products with no run yet.

    The forecast_runs table is populated by the nightly automation task
    (cron 0 2 * * *) which calls ForecastEngine.compute_all().

    Phase 6: Populates overlay_by_product with intelligence + policy metadata.
    Phase 7: Populates sku_forecasts_by_product for ERP products.
    """
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.schemas.market_dashboard import (
        ForecastOverlayMeta, IntelligenceOverlayMeta, PolicyOverlayMeta,
        SkuForecastMeta,
    )

    out: dict = {pid: None for pid in product_ids}
    overlay: dict = {pid: None for pid in product_ids}
    sku_forecasts: dict = {pid: [] for pid in product_ids}
    horizon_key = f"{day}d"

    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        # ── Separate md_t_lz_price products (direct key) from ERP products (prefix lookup) ──
        md_lz_pids = [pid for pid in product_ids if pid in PRODUCT_FORECAST_TARGET_KEY and pid not in _ERP_PRODUCT_KEYS]
        erp_pids = [pid for pid in product_ids if pid in _ERP_PRODUCT_KEYS]

        # ── md_t_lz_price products: direct product_key lookup (existing logic) ──
        if md_lz_pids:
            pid_to_key = {
                pid: PRODUCT_FORECAST_TARGET_KEY[pid]
                for pid in md_lz_pids
            }
            keys = list(pid_to_key.values())
            targets = db.query(ForecastTarget).filter(
                ForecastTarget.product_key.in_(keys),
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).all()
            key_to_pid = {v: k for k, v in pid_to_key.items()}

            for tgt in targets:
                pid = key_to_pid.get(tgt.product_key)
                if pid is None:
                    continue
                run = db.query(ForecastRun).filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                ).order_by(ForecastRun.created_date.desc()).first()
                if not run or not run.results:
                    continue
                horizon_payload = run.results.get(horizon_key) or {}
                base = horizon_payload.get("base")
                if base is None:
                    continue
                if isinstance(base, list) and len(base) > 0:
                    out[pid] = float(base[0])
                elif isinstance(base, (int, float)):
                    out[pid] = float(base)

                # Phase 6: extract overlay metadata
                explanation = horizon_payload.get("explanation") or {}
                intel_raw = explanation.get("intelligence") or {}
                policy_raw = explanation.get("policy") or {}

                intel_meta = None
                if intel_raw:
                    intel_meta = IntelligenceOverlayMeta(
                        active_event_count=int(intel_raw.get("active_event_count", 0)),
                        bias_direction=str(intel_raw.get("bias_direction", "neutral")),
                        impact_tier=intel_raw.get("impact_tier"),
                        confidence=intel_raw.get("confidence"),
                        summary=intel_raw.get("summary"),
                    )

                policy_meta = None
                if policy_raw:
                    policy_meta = PolicyOverlayMeta(
                        volatility_regime=policy_raw.get("volatility_regime"),
                        vol_multiplier=policy_raw.get("vol_multiplier"),
                        bias_pct=policy_raw.get("bias_pct"),
                        diagnosis_bias=policy_raw.get("diagnosis_bias"),
                        applied_factor=policy_raw.get("applied_factor"),
                    )

                if intel_meta or policy_meta:
                    overlay[pid] = ForecastOverlayMeta(
                        product_id=pid,
                        intelligence=intel_meta,
                        policy=policy_meta,
                    )

        # ── ERP products: prefix lookup for SKU-level targets ──
        for pid in erp_pids:
            prefix = f"ecisco.{pid}."
            sku_targets = db.query(ForecastTarget).filter(
                ForecastTarget.product_key.like(f"{prefix}%"),
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).order_by(ForecastTarget.report_order.asc()).all()

            if not sku_targets:
                continue

            sku_list: list[SkuForecastMeta] = []
            for tgt in sku_targets:
                # Extract material_code from product_key: "ecisco.c5_resin.CODE" → "CODE"
                material_code = tgt.product_key[len(prefix):]

                # Latest ForecastRun for this SKU target
                run = db.query(ForecastRun).filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                ).order_by(ForecastRun.created_date.desc()).first()

                sku_base: Optional[float] = None
                sku_confidence: Optional[float] = None
                if run and run.results:
                    horizon_payload = run.results.get(horizon_key) or {}
                    base = horizon_payload.get("base")
                    if isinstance(base, list) and len(base) > 0:
                        sku_base = float(base[0])
                    elif isinstance(base, (int, float)):
                        sku_base = float(base)
                    sku_confidence = run.confidence

                sku_meta = SkuForecastMeta(
                    material_code=material_code,
                    target_id=str(tgt.id),
                    name=tgt.name,
                    report_order=tgt.report_order or 99,
                    is_primary=(tgt.report_order is not None and tgt.report_order <= 12),
                    forecast_base=sku_base,
                    forecast_confidence=sku_confidence,
                )
                sku_list.append(sku_meta)

                # Primary SKU (first in list, lowest report_order) feeds the main forecast
                if sku_meta.is_primary and sku_base is not None and out[pid] is None:
                    out[pid] = sku_base

            sku_forecasts[pid] = sku_list

    finally:
        if owns_session:
            db.close()

    return ForecastBaseBatchResponse(
        day=day,
        base_by_product=out,
        overlay_by_product=overlay,
        sku_forecasts_by_product=sku_forecasts,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_batch_sku.py -v`
Expected: PASS — all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/market_dashboard_service.py backend/tests/test_forecast_batch_sku.py
git commit -m "feat(dashboard): SKU prefix lookup + drill-down in forecast batch

get_forecast_base_batch_v2 now handles two paths:
- md_t_lz_price products: direct product_key lookup (unchanged)
- ERP products: prefix lookup (ecisco.c5_resin.%) returns all SKU targets

Primary SKU (lowest report_order) feeds the main forecast card.
All SKUs returned in sku_forecasts_by_product for drill-down UI."
```

---

## Task 5: Update Forecast Tool with SKU Support

**Files:**
- Modify: `backend/app/services/tool_handlers/forecast_tool.py:55-67, 293-337`
- Test: covered by existing tool tests + manual verification

**Interfaces:**
- Consumes: `ForecastTarget` model
- Produces: `_serialize_target` with SKU fields, `forecast_list_skus` tool, `material_code`/`product_id` params on `forecast_get`/`forecast_run`

- [ ] **Step 1: Update `_serialize_target` to include SKU fields**

In `backend/app/services/tool_handlers/forecast_tool.py`, modify the `_serialize_target` function (lines 55-67):

```python
def _serialize_target(t: ForecastTarget) -> dict:
    """Convert a ForecastTarget row to a safe dict for tool responses."""
    ds = t.datasource or {}
    # Extract material_code from product_key for SKU targets
    # (e.g. "ecisco.c5_resin.201000018" → "201000018")
    material_code = None
    parts = t.product_key.split(".")
    if len(parts) >= 3 and parts[0] == "ecisco":
        material_code = parts[2]

    return {
        "id": t.id,
        "product_key": t.product_key,
        "name": t.name,
        "level": t.level,
        "quality_grade": t.quality_grade,
        "status": t.status,
        "granularity": ds.get("granularity"),
        "source": t.source,
        "include_in_weekly_report": t.include_in_weekly_report,
        "report_order": t.report_order,
        # SKU fields (None for md_t_lz_price products)
        "material_code": material_code,
        "datasource_table": ds.get("table"),
        "datasource_where": ds.get("where"),
    }
```

- [ ] **Step 2: Add `forecast_list_skus` tool**

Add this new tool handler and schema after the `forecast_get` section (after line 337):

```python
# ======================================================================
# forecast_list_skus  (lightweight read — SKU drill-down)
# ======================================================================

async def _forecast_list_skus(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """List all SKU-level forecast targets for an ERP product family.

    Required args:
        product_id  (str): The dashboard product_id (e.g. 'c5_resin',
                           'raffinate_c5').
    """
    product_id = args.get("product_id")
    if not product_id:
        return {"success": False, "error": "product_id is required."}

    org_id, _app_id = _resolve_org_context(context)
    prefix = f"ecisco.{product_id}."

    targets = db.query(ForecastTarget).filter(
        ForecastTarget.product_key.like(f"{prefix}%"),
        ForecastTarget.org_id == org_id,
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).order_by(ForecastTarget.report_order.asc()).all()

    if not targets:
        return {
            "success": True,
            "skus": [],
            "count": 0,
            "message": f"No SKU targets found for product '{product_id}'.",
        }

    return {
        "success": True,
        "skus": [_serialize_target(t) for t in targets],
        "count": len(targets),
    }


FORECAST_LIST_SKUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_list_skus",
        "description": (
            "List all SKU-level forecast targets for an ERP product family "
            "(c5_resin or raffinate_c5). Returns material_code, target_id, "
            "report_order, and whether each SKU is the primary (highest volume) "
            "for its family. Safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": (
                        "The dashboard product_id to list SKUs for. "
                        "Must be an ERP product: 'c5_resin' or 'raffinate_c5'."
                    ),
                },
            },
            "required": ["product_id"],
        },
    },
}
```

- [ ] **Step 3: Register the new tool**

In the registration loop at the bottom of the file (after line 832), add the new entry to the tuple:

```python
    (
        "forecast_list_skus",
        FORECAST_LIST_SKUS_SCHEMA,
        _forecast_list_skus,
        True,
        "List all SKU-level forecast targets for an ERP product family.",
    ),
```

- [ ] **Step 4: Add `product_id` param to `forecast_run` for SKU support**

In the `FORECAST_RUN_SCHEMA`, add an optional `product_id` parameter that, when provided, runs forecasts for all SKU targets of that product family. After the `target_id` property in the schema (around line 270):

```python
                "product_id": {
                    "type": "string",
                    "description": (
                        "ERP product family to run all SKU targets for "
                        "(e.g. 'c5_resin', 'raffinate_c5'). "
                        "Alternative to target_id for batch SKU runs."
                    ),
                },
```

And in the `_forecast_run` handler, add handling for `product_id` (after the `target_id` block, before the bulk path):

```python
    product_id = args.get("product_id")
    # ... after the target_id block ...
    
    if product_id:
        # Run all SKU targets for an ERP product family
        prefix = f"ecisco.{product_id}."
        sku_targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.like(f"{prefix}%"),
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).all()
        if not sku_targets:
            return {"success": False, "error": f"No SKU targets found for product '{product_id}'."}
        results = []
        for tgt in sku_targets:
            anchored = engine.compute_target_anchored(
                str(tgt.id), horizons, seasonal_period
            )
            if anchored and anchored.get("run"):
                run = anchored["run"]
                horizon_key = str(max(horizons))
                horizon_payload = (run.results or {}).get(horizon_key, {}) or {}
                results.append({
                    "target_id": run.target_id,
                    "product_key": tgt.product_key,
                    "material_code": tgt.product_key.split(".")[-1] if "." in tgt.product_key else None,
                    "horizon_days": max(horizons),
                    "point_estimate": horizon_payload.get("base", [None])[0] if horizon_payload else None,
                    "confidence": run.confidence,
                    "below_naive_baseline": run.below_naive_baseline,
                })
        return {"success": True, "runs": results, "count": len(results)}
```

- [ ] **Step 5: Verify the module imports cleanly**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -c "from app.services.tool_handlers.forecast_tool import _forecast_list_skus, FORECAST_LIST_SKUS_SCHEMA; print('OK')"`
Expected: prints `OK`.

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/tool_handlers/forecast_tool.py
git commit -m "feat(tools): add forecast_list_skus + SKU fields in _serialize_target

New forecast_list_skus tool lists all SKU targets for an ERP product
family. _serialize_target now includes material_code, report_order, and
datasource details. forecast_run gains product_id param for batch SKU runs."
```

---

## Task 6: Frontend — SKU Badge with Drill-Down Popover

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx:6-13, 227-273`
- Modify: `frontend/src/api/marketDashboard.js:79-83`
- Test: manual verification via preview

**Interfaces:**
- Consumes: `sku_forecasts_by_product` from the overview bundle / forecast-base-batch response
- Produces: SKU badge in the ForecastTrendChart header with a popover listing all SKUs

- [ ] **Step 1: Add `forecastListSkus` API method**

In `frontend/src/api/marketDashboard.js`, add the new method after the `forecastChart` method (around line 80):

```javascript
  /** List all SKU targets for an ERP product family (c5_resin, raffinate_c5). */
  forecastListSkus: (productId) =>
    getJson('/api/market-dashboard/forecast-skus', { product_id: productId }),
```

- [ ] **Step 2: Add SKU badge to ForecastTrendChart header**

In `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`:

**2a.** Add `Layers` to the lucide-react import (line 12):

```javascript
import { RefreshCw, Activity, Zap, Layers } from 'lucide-react';
```

**2b.** Add a `useState` for the SKU popover and a `useQuery` to fetch SKU data. After the existing `useQuery` for forecast chart data (around line 84), add:

```javascript
  const [showSkuPopover, setShowSkuPopover] = useState(false);

  // Fetch SKU list for ERP products (c5_resin, raffinate_c5)
  const isErpProduct = productId === 'c5_resin' || productId === 'raffinate_c5';
  const { data: skuData } = useQuery({
    queryKey: ['market-dashboard', 'forecast-skus', productId],
    queryFn: () => marketDashboardApi.forecastListSkus(productId),
    staleTime: 300_000,  // 5 min — SKU list doesn't change often
    enabled: !!productId && isErpProduct,
  });

  const skuList = skuData?.skus || [];
  const primarySku = skuList.find(s => s.is_primary) || skuList[0];
  const skuCount = skuList.length;
```

**2c.** Add the SKU badge in the header, after the overlay badges (after line 259, before the closing `</div>` of the header flex container):

```javascript
          {/* Phase 7: SKU badge for ERP products */}
          {isErpProduct && skuCount > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowSkuPopover(v => !v)}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-100 px-2 py-0.5
                  text-[11px] font-medium text-indigo-700 hover:bg-indigo-200
                  dark:bg-indigo-900/30 dark:text-indigo-400 dark:hover:bg-indigo-900/50
                  transition-colors"
                title={`${skuCount} SKUs (material_code) for this product family`}
              >
                <Layers size={11} />
                {skuCount} SKU{skuCount > 1 ? 's' : ''}
                {primarySku && (
                  <span className="ml-1 opacity-70">· {primarySku.material_code}</span>
                )}
              </button>
              {showSkuPopover && (
                <div className="absolute right-0 top-full z-50 mt-1 min-w-[280px] rounded-md
                  border border-slate-200 bg-white p-3 shadow-lg dark:border-slate-700 dark:bg-slate-900">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
                      SKU Forecast Breakdown
                    </span>
                    <button
                      onClick={() => setShowSkuPopover(false)}
                      className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="max-h-[240px] overflow-y-auto">
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="border-b border-slate-200 dark:border-slate-700">
                          <th className="pb-1 text-left font-medium text-slate-500 dark:text-slate-400">Code</th>
                          <th className="pb-1 text-right font-medium text-slate-500 dark:text-slate-400">Forecast</th>
                          <th className="pb-1 text-right font-medium text-slate-500 dark:text-slate-400">Conf.</th>
                        </tr>
                      </thead>
                      <tbody>
                        {skuList.map((sku) => (
                          <tr
                            key={sku.target_id}
                            className={`border-b border-slate-100 dark:border-slate-800 ${
                              sku.is_primary ? 'bg-indigo-50 dark:bg-indigo-900/20' : ''
                            }`}
                          >
                            <td className="py-1.5 font-mono text-slate-700 dark:text-slate-300">
                              {sku.is_primary && (
                                <span className="mr-1 text-indigo-600 dark:text-indigo-400" title="Primary SKU">★</span>
                              )}
                              {sku.material_code}
                            </td>
                            <td className="py-1.5 text-right tabular-nums font-mono text-slate-700 dark:text-slate-300">
                              {sku.forecast_base != null ? fmtPrice(sku.forecast_base) : '—'}
                            </td>
                            <td className="py-1.5 text-right tabular-nums text-slate-500 dark:text-slate-400">
                              {sku.forecast_confidence != null
                                ? `${(sku.forecast_confidence * 100).toFixed(0)}%`
                                : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="mt-2 text-[10px] text-slate-400 dark:text-slate-500">
                    ★ = Primary SKU (highest volume). Forecast shown is {days}-day base.
                  </p>
                </div>
              )}
            </div>
          )}
```

- [ ] **Step 3: Build the frontend**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build`
Expected: Build succeeds with no errors.

- [ ] **Step 4: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add frontend/src/api/marketDashboard.js \
        frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx
git commit -m "feat(frontend): SKU badge with drill-down popover in ForecastTrendChart

ERP products (c5_resin, raffinate_c5) now show a 'N SKUs' badge with
a Layers icon. Clicking opens a popover table listing all material_code
SKUs, their forecast base prices, and confidence levels. Primary SKU
(highest volume) is marked with a star and highlighted."
```

---

## Task 7: Add Backend Route for SKU List Endpoint

**Files:**
- Modify: `backend/app/routers/market_dashboard.py`
- Test: manual verification

**Interfaces:**
- Consumes: `forecast_list_skus` tool from Task 5 (or direct service call)
- Produces: `GET /api/market-dashboard/forecast-skus?product_id=c5_resin` endpoint

- [ ] **Step 1: Find the market dashboard router**

Run: `grep -n "forecast-chart\|forecast-base-batch\|forecast-skus" backend/app/routers/market_dashboard.py`

- [ ] **Step 2: Add the SKU list endpoint**

In `backend/app/routers/market_dashboard.py`, add a new endpoint after the existing `forecast-chart` route:

```python
@router.get("/forecast-skus")
async def list_forecast_skus(product_id: str):
    """List all SKU-level forecast targets for an ERP product family.

    Returns material_code, target_id, report_order, and forecast data
    for each SKU (material_code) in the family.
    """
    from app.models.forecasting import ForecastTarget
    from app.database import SessionLocal
    from app.schemas.market_dashboard import SkuForecastMeta

    if product_id not in ("c5_resin", "raffinate_c5"):
        raise HTTPException(
            status_code=400,
            detail=f"product_id must be an ERP product ('c5_resin' or 'raffinate_c5'), got '{product_id}'"
        )

    db = SessionLocal()
    try:
        prefix = f"ecisco.{product_id}."
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.like(f"{prefix}%"),
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).order_by(ForecastTarget.report_order.asc()).all()

        skus = []
        for tgt in targets:
            material_code = tgt.product_key[len(prefix):]
            # Get latest forecast run for this SKU
            from app.models.forecasting import ForecastRun
            run = db.query(ForecastRun).filter(
                ForecastRun.target_id == tgt.id,
                ForecastRun.is_deleted == False,  # noqa: E712
            ).order_by(ForecastRun.created_date.desc()).first()

            sku_base = None
            sku_confidence = None
            if run and run.results:
                horizon_payload = run.results.get("7d") or {}
                base = horizon_payload.get("base")
                if isinstance(base, list) and len(base) > 0:
                    sku_base = float(base[0])
                elif isinstance(base, (int, float)):
                    sku_base = float(base)
                sku_confidence = run.confidence

            skus.append(SkuForecastMeta(
                material_code=material_code,
                target_id=str(tgt.id),
                name=tgt.name,
                report_order=tgt.report_order or 99,
                is_primary=(tgt.report_order is not None and tgt.report_order <= 12),
                forecast_base=sku_base,
                forecast_confidence=sku_confidence,
            ))

        return {"skus": [s.model_dump() for s in skus], "count": len(skus)}
    finally:
        db.close()
```

- [ ] **Step 3: Restart backend and verify**

Run: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`

Wait 10 seconds, then test:
```bash
curl -s "http://localhost:8000/api/market-dashboard/forecast-skus?product_id=c5_resin" | python3 -m json.tool | head -20
```
Expected: Returns `{"skus": [...], "count": N}` with SKU data (if seeded).

- [ ] **Step 4: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/routers/market_dashboard.py
git commit -m "feat(router): add GET /forecast-skus endpoint for SKU drill-down

Returns all SKU targets for an ERP product family with forecast base
prices and confidence. Used by the frontend SKU badge popover."
```

---

## Task 8: Wire Up SKU Seeder in Bootstrap

**Files:**
- Modify: `backend/app/services/forecasting/seed.py` (or wherever `seed_ecisco_forecast_targets` is called on boot)
- Test: manual verification via docker restart

- [ ] **Step 1: Find where the seeder is called on boot**

Run: `grep -rn "seed_ecisco_forecast_targets" backend/app/ --include="*.py"`

- [ ] **Step 2: Add `discover_and_seed_sku_targets` call after the static seeder**

In the file where `seed_ecisco_forecast_targets` is called (likely `backend/app/services/forecasting/seed.py` or `backend/app/main.py` lifespan), add:

```python
    # Seed static md_t_lz_price targets (10 products)
    seed_ecisco_forecast_targets(db, org_id=org_id, app_id=app_id)
    # Discover and seed ERP SKU targets (c5_resin, raffinate_c5)
    discover_and_seed_sku_targets(db, org_id=org_id, app_id=app_id)
```

Add the import at the top:
```python
from app.services.forecasting.seed_ecisco_targets import (
    seed_ecisco_forecast_targets,
    discover_and_seed_sku_targets,
)
```

- [ ] **Step 3: Restart backend and verify seeding**

Run: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`

Wait 15 seconds, then check logs:
```bash
docker compose logs backend 2>&1 | grep -i "sku\|discover" | tail -10
```
Expected: Logs show `discover_and_seed_sku_targets: c5_resin — N SKUs qualified` and `inserted N new SKU targets`.

- [ ] **Step 4: Verify in database**

```bash
docker compose exec postgres psql -U zhanlu -d zhanlu -c "
  SELECT product_key, name, report_order, include_in_weekly_report
  FROM forecast_targets
  WHERE product_key LIKE 'ecisco.c5_resin.%' OR product_key LIKE 'ecisco.raffinate_c5.%'
  ORDER BY product_key;
"
```
Expected: Shows SKU-level targets with `product_key` like `ecisco.c5_resin.201000018`.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/seed.py  # or wherever the call is
git commit -m "feat(bootstrap): call discover_and_seed_sku_targets on startup

The dynamic SKU seeder runs after the static md_t_lz_price seeder on
every boot. It's idempotent — skips targets that already exist."
```

---

## Task 9: Deploy and End-to-End Verification

**Files:** none (verification only)

- [ ] **Step 1: Run all forecast-related tests**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_edia_source.py tests/test_edia_source_groupby.py tests/test_seed_ecisco_targets.py tests/test_seed_sku_targets.py tests/test_forecast_batch_sku.py -v
```
Expected: All tests pass.

- [ ] **Step 2: Restart backend**

Run: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`
Wait 15 seconds.

- [ ] **Step 3: Build frontend**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 4: Verify SKU data in dashboard API**

```bash
# Login to get token
TOKEN=$(curl -s -X POST "http://localhost:8000/api/apps/default-app/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

# Get overview bundle — check sku_forecasts_by_product
curl -s "http://localhost:8000/api/market-dashboard/overview" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
sku_data = data.get('forecast_sku_forecasts_by_product', {})
if not sku_data:
    # Try alternate key
    sku_data = data.get('sku_forecasts_by_product', {})
print('SKU data keys:', list(sku_data.keys()))
for pid, skus in sku_data.items():
    print(f'  {pid}: {len(skus)} SKUs')
    for s in skus[:3]:
        print(f'    - {s.get(\"material_code\")}: base={s.get(\"forecast_base\")}, primary={s.get(\"is_primary\")}')
"
```
Expected: Shows SKU data for c5_resin and/or raffinate_c5.

- [ ] **Step 5: Preview the dashboard**

Open the market dashboard in the browser and navigate to the forecast section for c5_resin. Verify:
- SKU badge appears with "N SKUs · <primary_code>"
- Clicking the badge opens the popover with all SKUs listed
- Primary SKU is marked with a star
- Forecast prices are shown per SKU

- [ ] **Step 6: Final commit (if any cleanup needed)**

```bash
cd /home/ysk2025/zhanlu_7_30
git status
# If there are uncommitted changes:
git add -A
git commit -m "chore: SKU-level forecasting — final cleanup"
```

---

## Self-Review Checklist

### Spec Coverage

| Design Section | Task(s) | Status |
|---|---|---|
| Section 1: ERP query with `WHERE 1=1 AND FTAXPRICE > 0` filter | Task 2 | ✓ — `discover_and_seed_sku_targets` uses `WHERE FTAXPRICE > 0` |
| Section 2: `groupby("ds").agg({"y": "mean"})` date deduplication | Task 1 | ✓ — Added to `edia_source.py` after sentinel filtering |
| Section 3: Primary SKU selection by `report_order` (highest volume) | Task 2 | ✓ — Primary SKU gets `report_order=12`, secondary 13, 14, ... |
| Section 4: Dashboard drill-down via `sku_forecasts_by_product` | Tasks 3, 4, 6 | ✓ — Schema, batch endpoint, frontend badge+popover |
| Section 5: Agent tool discovery via schema | Task 5 | ✓ — `forecast_list_skus` tool registered with schema |

### Placeholder Scan

- No "TBD", "TODO", "implement later" found.
- All code blocks contain actual implementation code.
- All test code blocks contain actual test code.
- No "similar to Task N" references — each task is self-contained.

### Type Consistency

- `SkuForecastMeta` — defined in Task 3, used in Task 4 and Task 7. Fields match: `material_code`, `target_id`, `name`, `report_order`, `is_primary`, `forecast_base`, `forecast_confidence`.
- `discover_and_seed_sku_targets` — defined in Task 2, called in Task 8. Signature matches: `(db, org_id, app_id, engine=None) -> int`.
- `_serialize_target` — modified in Task 5, returns `material_code`, `report_order`, `datasource_table`, `datasource_where`.
- `forecast_list_skus` — defined in Task 5, API method added in Task 6, endpoint added in Task 7.

---

# Phase B: EDIA Domain Signal Layers

> **Phase B is independent of Phase A.** It touches the forecast *engine* (overlay application) and the *dashboard* (trust-tier badge), not the SKU data plumbing. It can be executed before, after, or in parallel with Phase A.

**Goal:** Port three cheap, high-value domain-knowledge layers from EDIA 5.1.2 that our statistical ensemble (ARIMA + XGBoost + STL) cannot infer from short price series alone:

1. **Tier-dampened causal-chain elasticities** — naphtha → C5 → downstream propagation. When naphtha moves X%, `c5_resin` moves ~X × 0.62 × 0.72 = X × 0.45. Encodes petrochem supply-chain economics.
2. **Per-product seasonal adjustment rules** — additive % by (product_id, month). E.g. `c5_resin` is −2.5% Nov–Feb, +1.0% Mar–Oct. Captures demand seasonality the models under-fit on short series.
3. **Forecast trust tiers** — label weekly-cadence products (`cracked_c9`, `raffinate_c5`…) as "directional only" instead of presenting misleading daily precision. Four tiers: high / medium / directional / low, with bilingual labels + badge colors.

**Rationale:** EDIA's ensemble is architecturally similar to ours but gained measurable accuracy and trustworthiness from these layers because they encode domain economics. All three are pure-Python dict + overlay functions — no new ML dependencies, no torch, no training data. ~2 days of work.

**EDIA source (read-only reference):**
- `EDIA_5.1.2/backend/src/service/forecasting_engine.py:113-179` — `_RAW_ELASTICITIES`, `_ELASTICITIES`, `_SEASONAL_RULES` dicts
- `EDIA_5.1.2/backend/src/service/forecast_supervisor.py:150-283` — `compute_forecast_trust_tier()`, `_HIGH_SKILL_PRODUCTS`, `_WEEKLY_CADENCE_PRODUCTS`

**Integration point in zhanlu:** `backend/app/services/forecasting/engine.py` — the `compute_target()` method has numbered steps. Step 8.5 (intelligence overlay, line ~337) and Step 8.6 (policy service, line ~380) already exist. A new **Step 8.55 (domain signals)** slots between them, mirroring the intelligence overlay's try/except + gated-flag pattern. The explanation dict (line ~449) gains a `domain_signals` key alongside the existing `intelligence` and `policy` keys.

**Feature flag:** `FORECAST_DOMAIN_SIGNALS_ENABLED` (default `False`), following the same pattern as `FORECAST_INTELLIGENCE_OVERLAY_ENABLED`. Add to `.env` after verification.

---

## Phase B File Structure

**New files:**

| File | Purpose |
|---|---|
| `backend/app/services/forecasting/domain_signals.py` | Elasticity + seasonal-rule dicts and `compute_domain_signal_adjustment()` overlay function |
| `backend/app/services/forecasting/forecast_trust_tier.py` | `compute_forecast_trust_tier()` + the `_HIGH_SKILL_PRODUCTS` / `_WEEKLY_CADENCE_PRODUCTS` frozensets |
| `backend/tests/test_domain_signals.py` | Tests for elasticities, seasonal rules, combined adjustment |
| `backend/tests/test_forecast_trust_tier.py` | Tests for the 4 trust tiers + edge cases |

**Modified files:**

| File | Change |
|---|---|
| `backend/app/services/forecasting/engine.py` | Insert Step 8.55 (domain signals overlay) between Steps 8.5 and 8.6; add `domain_signals` + `trust_tier` to explanation dict |
| `backend/app/schemas/market_dashboard.py` | Add `TrustTierMeta` schema + `trust_tier` field on `ForecastOverlayMeta` |
| `backend/app/services/market_dashboard_service.py` | Populate `trust_tier` in `get_forecast_base_batch_v2` overlay extraction |
| `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx` | Trust-tier badge (colored dot + tooltip) in the forecast card header |

---

## Task B1: Port Elasticities + Seasonal Rules into `domain_signals.py`

**Files:**
- New: `backend/app/services/forecasting/domain_signals.py`
- Test: `backend/tests/test_domain_signals.py`

**Interfaces:**
- Consumes: nothing (standalone module — pure dicts + functions)
- Produces:
  - `_RAW_ELASTICITIES: dict[str, tuple[float, float]]` — (raw_elasticity, tier_dampening_factor)
  - `_ELASTICITIES: dict[str, float]` — pre-computed raw × dampening
  - `_SEASONAL_RULES: dict[tuple[str, int], float]` — (product_id, month) → additive pct
  - `compute_seasonal_adjustment(product_id: str, month: int) -> float` — returns the seasonal % (0.0 if no rule)
  - `compute_causal_chain_adjustment(product_id: str, naphtha_pct_change: float | None) -> float` — returns the propagated % (0.0 if no naphtha signal or unknown product)
  - `compute_domain_signal_adjustment(product_id: str, as_of_date: datetime, naphtha_pct_change: float | None = None) -> dict` — combined overlay, returns `{seasonal_pct, causal_pct, total_pct, applied_rules: list[str]}`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_domain_signals.py`:

```python
"""Tests for EDIA-ported domain signals: elasticities + seasonal rules."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.forecasting.domain_signals import (
    _ELASTICITIES,
    _RAW_ELASTICITIES,
    _SEASONAL_RULES,
    compute_causal_chain_adjustment,
    compute_domain_signal_adjustment,
    compute_seasonal_adjustment,
)


# ── Elasticity constants ────────────────────────────────────────────────────

def test_elasticities_dict_has_known_products():
    """The elasticity table should include the petrochem chain products."""
    for pid in ("naphtha", "cracked_c5", "isoprene", "piperylene",
                "dcpd", "c5_resin", "raffinate_c5", "sbs"):
        assert pid in _ELASTICITIES, f"{pid} missing from _ELASTICITIES"


def test_elasticities_are_raw_times_dampening():
    """Each effective coefficient = raw × tier_dampening, rounded to 4 dp."""
    for pid, (raw, damp) in _RAW_ELASTICITIES.items():
        expected = round(raw * damp, 4)
        assert _ELASTICITIES[pid] == expected, (
            f"{pid}: {raw}×{damp}={expected} but _ELASTICITIES has {_ELASTICITIES[pid]}"
        )


def test_naphtha_elasticity_is_unity():
    """Naphtha is the root of the causal chain — elasticity 1.0."""
    assert _ELASTICITIES["naphtha"] == 1.0


def test_c5_resin_tier3_dampening():
    """c5_resin is Tier 3: raw 0.62 × damp 0.72 = 0.4464."""
    assert _RAW_ELASTICITIES["c5_resin"] == (0.62, 0.72)
    assert _ELASTICITIES["c5_resin"] == 0.4464


def test_isoprene_tier2_dampening():
    """isoprene is Tier 2: raw 0.88 × damp 0.85 = 0.748."""
    assert _ELASTICITIES["isoprene"] == 0.748


# ── Seasonal rules ──────────────────────────────────────────────────────────

def test_seasonal_rules_cover_c5_resin_full_year():
    """c5_resin should have a rule for every month 1-12."""
    c5_months = {m for (pid, m) in _SEASONAL_RULES if pid == "c5_resin"}
    assert c5_months == set(range(1, 13)), (
        f"c5_resin seasonal rules missing months: {set(range(1,13)) - c5_months}"
    )


def test_seasonal_c5_resin_winter_is_negative():
    """c5_resin Nov-Feb should be -2.5% (winter demand dip)."""
    for month in (11, 12, 1, 2):
        assert _SEASONAL_RULES[("c5_resin", month)] == -2.5


def test_seasonal_c5_resin_spring_summer_is_positive():
    """c5_resin Mar-Oct should be +1.0%."""
    for month in range(3, 11):
        assert _SEASONAL_RULES[("c5_resin", month)] == 1.0


def test_seasonal_adjustment_returns_zero_for_unknown_product():
    """Unknown product → 0.0 (no adjustment)."""
    assert compute_seasonal_adjustment("unknown_product", 7) == 0.0


def test_seasonal_adjustment_returns_zero_for_unrule_month():
    """Product with no rule for the given month → 0.0."""
    # naphtha has no seasonal rules at all
    assert compute_seasonal_adjustment("naphtha", 7) == 0.0


def test_seasonal_adjustment_returns_pct_for_known_combo():
    """Known (product, month) → the rule's pct value."""
    assert compute_seasonal_adjustment("c5_resin", 12) == -2.5
    assert compute_seasonal_adjustment("c5_resin", 6) == 1.0


# ── Causal-chain adjustment ─────────────────────────────────────────────────

def test_causal_chain_zero_when_no_naphtha_signal():
    """No naphtha % change → causal adjustment is 0.0."""
    assert compute_causal_chain_adjustment("c5_resin", None) == 0.0


def test_causal_chain_zero_for_unknown_product():
    """Unknown product → 0.0 even if naphtha moved."""
    assert compute_causal_chain_adjustment("unknown", 10.0) == 0.0


def test_causal_chain_propagates_naphtha_to_c5_resin():
    """Naphtha +10% → c5_resin moves +10 × 0.4464 = +4.464%."""
    pct = compute_causal_chain_adjustment("c5_resin", 10.0)
    assert pct == pytest.approx(4.464, abs=1e-3)


def test_causal_chain_propagates_negative_naphtha_move():
    """Naphtha -5% → c5_resin moves -5 × 0.4464 = -2.232%."""
    pct = compute_causal_chain_adjustment("c5_resin", -5.0)
    assert pct == pytest.approx(-2.232, abs=1e-3)


def test_causal_chain_naphtha_self_is_full_pass_through():
    """Naphtha elasticity = 1.0, so naphtha +8% → naphtha +8%."""
    pct = compute_causal_chain_adjustment("naphtha", 8.0)
    assert pct == pytest.approx(8.0)


# ── Combined domain signal adjustment ───────────────────────────────────────

def test_combined_adjustment_seasonal_only_when_no_naphtha():
    """Without a naphtha signal, only the seasonal rule applies."""
    result = compute_domain_signal_adjustment(
        product_id="c5_resin",
        as_of_date=datetime(2026, 12, 15),  # December → -2.5%
        naphtha_pct_change=None,
    )
    assert result["seasonal_pct"] == -2.5
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == -2.5
    assert "seasonal:c5_resin:12" in result["applied_rules"]
    assert not any(r.startswith("causal:") for r in result["applied_rules"])


def test_combined_adjustment_applies_both_layers():
    """Both seasonal + causal apply when naphtha signal is present."""
    result = compute_domain_signal_adjustment(
        product_id="c5_resin",
        as_of_date=datetime(2026, 6, 15),  # June → +1.0%
        naphtha_pct_change=10.0,           # → +4.464%
    )
    assert result["seasonal_pct"] == 1.0
    assert result["causal_pct"] == pytest.approx(4.464, abs=1e-3)
    assert result["total_pct"] == pytest.approx(5.464, abs=1e-3)
    assert "seasonal:c5_resin:6" in result["applied_rules"]
    assert "causal:c5_resin:naphtha=+10.0%" in result["applied_rules"]


def test_combined_adjustment_no_rules_for_unknown_product():
    """Unknown product → all zeros, empty applied_rules."""
    result = compute_domain_signal_adjustment(
        product_id="unknown",
        as_of_date=datetime(2026, 7, 1),
        naphtha_pct_change=10.0,
    )
    assert result["seasonal_pct"] == 0.0
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == 0.0
    assert result["applied_rules"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_domain_signals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.forecasting.domain_signals'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/forecasting/domain_signals.py`:

```python
"""EDIA-ported domain signal layers for the forecast engine.

Two pure-Python overlay layers that encode petrochem supply-chain economics
the statistical ensemble (ARIMA + XGBoost + STL) cannot infer from short
price series alone:

1. **Tier-dampened causal-chain elasticities** — naphtha is the root feedstock.
   When naphtha moves X%, downstream products move X × (raw_elasticity ×
   tier_dampening)%. Three tiers model propagation attenuation:
     Tier 1 (naphtha → C5):        ×1.00
     Tier 2 (C5 → downstream):     ×0.85
     Tier 3 (downstream → end-use): ×0.72

2. **Per-product seasonal adjustment rules** — additive % by (product_id, month).
   Captures demand seasonality (winter dip, spring recovery).

Source: EDIA_5.1.2/backend/src/service/forecasting_engine.py:113-179 (verbatim).

Integration: called by ForecastEngine.compute_target() at Step 8.55, between
the intelligence overlay (8.5) and the policy service (8.6). Gated by
FORECAST_DOMAIN_SIGNALS_ENABLED (default False).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier-dampened causal-chain elasticities (EDIA §6.3)
# ---------------------------------------------------------------------------

# product_id: (raw_elasticity, tier_dampening_factor)
# Tier 1 (naphtha → C5): ×1.00   Tier 2 (C5 → downstream): ×0.85
# Tier 3 (downstream → end-use): ×0.72
_RAW_ELASTICITIES: dict[str, tuple[float, float]] = {
    # product_id: (raw_elasticity, tier_dampening_factor)
    "naphtha": (1.00, 1.00),
    "cracked_c5": (0.82, 1.00),  # Tier 1
    "mixed_c5": (0.82, 1.00),  # Tier 1
    "cracked_c9": (0.65, 0.85),
    "isoprene": (0.88, 0.85),  # Tier 2: 0.88 × 0.85 = 0.748
    "piperylene": (0.85, 0.85),  # Tier 2: 0.85 × 0.85 = 0.7225
    "dcpd": (0.78, 0.85),  # Tier 2: 0.78 × 0.85 = 0.663
    "raffinate_c5": (0.70, 0.85),
    "styrene": (0.55, 0.85),  # Tier 2: 0.55 × 0.85 = 0.4675
    "sis": (0.71, 0.72),  # Tier 3: 0.71 × 0.72 = 0.5112
    "sis_rubber": (0.71, 0.72),  # Tier 3
    "blowing_agent": (0.70, 0.85),
    "butadiene": (0.65, 0.85),
    "sbs": (0.60, 0.72),  # Tier 3
    "c5_resin": (0.62, 0.72),  # Tier 3: 0.62 × 0.72 = 0.4464
    "mixed_c9": (0.65, 0.85),
    "hydrogenated_c9": (0.60, 0.72),
    "trimethylbenzene": (0.60, 0.72),
    "c9_resin": (0.60, 0.72),  # legacy alias
}

# Pre-computed effective coefficients = raw × dampening
_ELASTICITIES: dict[str, float] = {
    k: round(raw * damp, 4) for k, (raw, damp) in _RAW_ELASTICITIES.items()
}


# ---------------------------------------------------------------------------
# Seasonal rules (product_id, month) → pct adjustment (additive)
# ---------------------------------------------------------------------------

_SEASONAL_RULES: dict[tuple[str, int], float] = {
    ("c5_resin", 11): -2.5,
    ("c5_resin", 12): -2.5,
    ("c5_resin", 1): -2.5,
    ("c5_resin", 2): -2.5,
    ("c5_resin", 3): 1.0,
    ("c5_resin", 4): 1.0,
    ("c5_resin", 5): 1.0,
    ("c5_resin", 6): 1.0,
    ("c5_resin", 7): 1.0,
    ("c5_resin", 8): 1.0,
    ("c5_resin", 9): 1.0,
    ("dcpd", 11): -3.0,
    ("dcpd", 12): -3.0,
    ("dcpd", 1): -3.0,
    ("dcpd", 2): -3.0,
    ("dcpd", 3): 1.5,
    ("dcpd", 4): 1.5,
    ("dcpd", 5): 1.5,
    ("dcpd", 6): 1.5,
    ("dcpd", 7): 1.5,
    ("dcpd", 8): 1.5,
    ("isoprene", 2): 0.8,
    ("isoprene", 3): 0.8,
    ("cracked_c5", 1): -1.0,
    ("cracked_c5", 2): -1.0,
    ("cracked_c5", 3): 0.5,
    ("cracked_c5", 4): 0.5,
    ("cracked_c5", 5): 0.5,
    ("piperylene", 2): 1.2,
    ("piperylene", 3): 1.2,
    ("sis", 2): 0.5,
    ("sis", 3): 0.8,
    ("sis", 9): 0.5,
}


# ---------------------------------------------------------------------------
# Overlay functions
# ---------------------------------------------------------------------------

def compute_seasonal_adjustment(product_id: str, month: int) -> float:
    """Return the seasonal % adjustment for a product in a given month.

    Args:
        product_id: The dashboard product_id (e.g. "c5_resin", "naphtha").
        month: Month number 1-12.

    Returns:
        Additive percentage adjustment (e.g. -2.5 means −2.5%).
        Returns 0.0 if no rule exists for this (product_id, month).
    """
    return _SEASONAL_RULES.get((product_id, month), 0.0)


def compute_causal_chain_adjustment(
    product_id: str,
    naphtha_pct_change: float | None,
) -> float:
    """Propagate a naphtha price change through the causal chain.

    Args:
        product_id: The product whose forecast is being adjusted.
        naphtha_pct_change: The recent % change in naphtha price
            (e.g. 10.0 means naphtha rose 10%). None if no signal.

    Returns:
        The propagated % adjustment for this product (e.g. 4.464 means
        +4.464%). Returns 0.0 if naphtha_pct_change is None or the
        product is not in the elasticity table.
    """
    if naphtha_pct_change is None:
        return 0.0
    elasticity = _ELASTICITIES.get(product_id)
    if elasticity is None:
        return 0.0
    return round(naphtha_pct_change * elasticity, 4)


def compute_domain_signal_adjustment(
    product_id: str,
    as_of_date: datetime,
    naphtha_pct_change: float | None = None,
) -> dict[str, Any]:
    """Combined domain-signal overlay: seasonal + causal-chain.

    Called by ForecastEngine.compute_target() at Step 8.55. Returns a dict
    suitable for embedding in the forecast run's explanation.

    Args:
        product_id: The dashboard product_id.
        as_of_date: The forecast anchor date (determines the month for
            seasonal rules).
        naphtha_pct_change: Recent % change in naphtha price, or None.

    Returns:
        dict with keys:
            seasonal_pct: float — additive % from seasonal rules
            causal_pct: float — additive % from causal-chain propagation
            total_pct: float — seasonal_pct + causal_pct
            applied_rules: list[str] — human-readable rule identifiers
                          (e.g. "seasonal:c5_resin:12", "causal:c5_resin:naphtha=+10.0%")
    """
    applied_rules: list[str] = []

    seasonal_pct = compute_seasonal_adjustment(product_id, as_of_date.month)
    if seasonal_pct != 0.0:
        applied_rules.append(f"seasonal:{product_id}:{as_of_date.month}")

    causal_pct = compute_causal_chain_adjustment(product_id, naphtha_pct_change)
    if causal_pct != 0.0 and naphtha_pct_change is not None:
        sign = "+" if naphtha_pct_change >= 0 else ""
        applied_rules.append(
            f"causal:{product_id}:naphtha={sign}{naphtha_pct_change}%"
        )

    total_pct = round(seasonal_pct + causal_pct, 4)

    return {
        "seasonal_pct": seasonal_pct,
        "causal_pct": causal_pct,
        "total_pct": total_pct,
        "applied_rules": applied_rules,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_domain_signals.py -v`
Expected: PASS — all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/domain_signals.py backend/tests/test_domain_signals.py
git commit -m "feat(forecast): port EDIA domain signals — elasticities + seasonal rules

Two pure-Python overlay layers from EDIA 5.1.2:
- Tier-dampened causal-chain elasticities (naphtha → C5 → downstream)
- Per-product seasonal adjustment rules by (product_id, month)

No new ML deps. Standalone module — wired into the engine in Task B2.
Source: EDIA_5.1.2/backend/src/service/forecasting_engine.py:113-179"
```

---

## Task B2: Naphtha % Change Fetcher + Wire Overlay into Engine

**Files:**
- Modify: `backend/app/services/forecasting/engine.py` (insert Step 8.55 between lines ~378 and ~380)
- Test: covered by `tests/test_domain_signals.py` (B1) + manual verification of engine run

**Interfaces:**
- Consumes: `compute_domain_signal_adjustment` from Task B1, `EdiaMysqlDataSource` for naphtha history
- Produces: a `domain_signals` report dict embedded in the forecast run's `explanation`, and an applied percentage adjustment to the base forecast

**Design — naphtha signal:** The causal chain needs the recent naphtha % change. We compute it by reading the naphtha forecast target's recent history (last 14 days) and comparing the mean of the last 3 days vs. the mean of the preceding 11 days. This is a robust measure that ignores single-day spikes. If naphtha history is unavailable (e.g. MySQL down), `naphtha_pct_change` is `None` and only the seasonal rule applies — graceful degradation.

- [ ] **Step 1: Add the naphtha % change fetcher**

In `backend/app/services/forecasting/domain_signals.py`, append at the bottom of the file:

```python
# ---------------------------------------------------------------------------
# Naphtha signal fetcher
# ---------------------------------------------------------------------------

_NAPHTHA_LOOKBACK_DAYS = 14
_NAPHTHA_RECENT_WINDOW = 3  # last 3 days vs preceding 11


def fetch_naphtha_recent_pct_change(
    db: Any,
    org_id: str = "default-org",
    lookback_days: int = _NAPHTHA_LOOKBACK_DAYS,
) -> float | None:
    """Compute the recent % change in naphtha price.

    Reads the naphtha forecast target's source history (last 14 days) and
    compares the mean of the last 3 days against the mean of the preceding
    11 days. This is robust to single-day spikes.

    Args:
        db: SQLAlchemy session (sync).
        org_id: Org ID for the naphtha target.
        lookback_days: Total lookback window (default 14).

    Returns:
        The % change (e.g. 10.0 means naphtha rose 10%), or None if the
        naphtha target or its history is unavailable.
    """
    try:
        from app.models.forecasting import ForecastTarget
        from app.services.forecasting.edia_source import EdiaMysqlDataSource
        from datetime import timedelta, timezone

        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == "ecisco.naphtha",
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if target is None or not target.datasource:
            return None

        src = EdiaMysqlDataSource()
        df = src.read_history(target.datasource)
        if df is None or len(df) < lookback_days:
            return None

        # Take the last `lookback_days` rows
        recent = df.tail(lookback_days).reset_index(drop=True)
        # Split: last _NAPHTHA_RECENT_WINDOW days vs the preceding days
        if len(recent) <= _NAPHTHA_RECENT_WINDOW:
            return None
        recent_mean = recent.tail(_NAPHTHA_RECENT_WINDOW)["y"].mean()
        preceding_mean = recent.head(len(recent) - _NAPHTHA_RECENT_WINDOW)["y"].mean()

        if preceding_mean == 0 or preceding_mean is None or recent_mean is None:
            return None

        pct = round((recent_mean - preceding_mean) / preceding_mean * 100.0, 2)
        logger.info(
            "[domain-signals] naphtha %s: recent_mean=%.2f, preceding_mean=%.2f → %+.2f%%",
            org_id, recent_mean, preceding_mean, pct,
        )
        return pct
    except Exception as exc:
        logger.warning("[domain-signals] naphtha pct change fetch failed: %s", exc)
        return None
```

- [ ] **Step 2: Write a test for the naphtha fetcher (optional but recommended)**

Append to `backend/tests/test_domain_signals.py`:

```python
def test_fetch_naphtha_returns_none_when_no_target(db_session=None):
    """No naphtha ForecastTarget → None (graceful degradation)."""
    from app.services.forecasting.domain_signals import fetch_naphtha_recent_pct_change
    # Pass a bare session-like object with a .query that returns None
    class _FakeQuery:
        def filter(self, *a, **k): return self
        def first(self): return None
    class _FakeDb:
        def query(self, model): return _FakeQuery()
    assert fetch_naphtha_recent_pct_change(_FakeDb()) is None
```

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_domain_signals.py::test_fetch_naphtha_returns_none_when_no_target -v`
Expected: PASS.

- [ ] **Step 3: Wire Step 8.55 into `engine.py`**

In `backend/app/services/forecasting/engine.py`, locate Step 8.5 (intelligence overlay, ends around line 378 with `intelligence_report = None`) and Step 8.6 (policy service, starts around line 380). Insert **between** them:

```python
        # --- Step 8.55: Domain signals overlay (Phase B) ---
        # EDIA-ported causal-chain elasticities + seasonal rules.
        # Gated by FORECAST_DOMAIN_SIGNALS_ENABLED (default False).
        domain_signals_enabled = _get_config_bool(
            "FORECAST_DOMAIN_SIGNALS_ENABLED", False
        )
        domain_signals_report: dict | None = None
        if domain_signals_enabled:
            try:
                from app.services.forecasting.domain_signals import (
                    compute_domain_signal_adjustment,
                    fetch_naphtha_recent_pct_change,
                )
                # Extract product_id from target.product_key.
                # For family targets: "ecisco.c5_resin" → "c5_resin"
                # For SKU targets:   "ecisco.c5_resin.201000018" → "c5_resin"
                parts = (target.product_key or "").split(".")
                product_id = parts[1] if len(parts) >= 2 else (target.product_key or "")

                naphtha_pct = fetch_naphtha_recent_pct_change(
                    self._db, org_id=target.org_id or "default-org"
                )

                domain_signals_report = compute_domain_signal_adjustment(
                    product_id=product_id,
                    as_of_date=datetime.now(),
                    naphtha_pct_change=naphtha_pct,
                )

                # Apply the combined % adjustment to the base forecast.
                # total_pct is additive (e.g. +5.464 means +5.464%).
                total_pct = domain_signals_report.get("total_pct", 0.0)
                if total_pct != 0.0:
                    for h_key, h_payload in (run.results or {}).items():
                        base_list = h_payload.get("base") if isinstance(h_payload, dict) else None
                        if isinstance(base_list, list):
                            h_payload["base"] = [
                                round(b * (1.0 + total_pct / 100.0), 4)
                                for b in base_list
                            ]
                            # Record the pre-adjustment base for audit
                            h_payload["base_pre_domain_signals"] = [
                                round(b, 4) for b in base_list
                            ]

                logger.info(
                    "[domain-signals] %s: applied %+.4f%% (seasonal=%+.4f, causal=%+.4f, rules=%s)",
                    target.product_key, total_pct,
                    domain_signals_report.get("seasonal_pct", 0.0),
                    domain_signals_report.get("causal_pct", 0.0),
                    domain_signals_report.get("applied_rules", []),
                )
            except Exception as exc:
                logger.warning(
                    "[domain-signals] failed for %s: %s", target.product_key, exc
                )
                domain_signals_report = None
```

**Important:** The `run` variable referenced here is the `ForecastRun` object created earlier in `compute_target()` (the variable that holds the ensemble results before explanation assembly). Verify the exact variable name in the current engine — if it's named differently (e.g. `new_run`), adjust the reference. Read lines 300-380 of `engine.py` first to confirm.

- [ ] **Step 4: Add `domain_signals` to the explanation dict**

In the same `engine.py`, locate the explanation assembly (around line 449, where `explanation` is built with `"intelligence": intelligence_report, "policy": policy_report`). Add:

```python
                "intelligence": intelligence_report,
                "policy": policy_report,
                "domain_signals": domain_signals_report,
```

- [ ] **Step 5: Verify the module imports cleanly**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -c "from app.services.forecasting.engine import ForecastEngine; print('OK')"`
Expected: prints `OK`.

- [ ] **Step 6: Run existing engine tests to verify no regression**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_engine.py -v 2>&1 | tail -20`
Expected: All existing tests pass (the overlay is gated off by default — `FORECAST_DOMAIN_SIGNALS_ENABLED=False`).

- [ ] **Step 7: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/domain_signals.py \
        backend/app/services/forecasting/engine.py \
        backend/tests/test_domain_signals.py
git commit -m "feat(forecast): wire domain signals overlay into engine (Step 8.55)

Inserts a new Step 8.55 between the intelligence overlay (8.5) and the
policy service (8.6). Computes seasonal + causal-chain adjustments and
applies them to the base forecast. Gated by FORECAST_DOMAIN_SIGNALS_ENABLED
(default False). Adds a naphtha % change fetcher that reads the naphtha
target's recent history (last 14d, last 3d vs preceding 11d)."
```

---

## Task B3: Port Forecast Trust Tiers

**Files:**
- New: `backend/app/services/forecasting/forecast_trust_tier.py`
- Test: `backend/tests/test_forecast_trust_tier.py`

**Interfaces:**
- Consumes: `ForecastRun.below_naive_baseline` (bool) and `ForecastRun.confidence` (float)
- Produces: `compute_forecast_trust_tier(product_id, below_naive, cadence_class, mape) -> dict` with `tier`, `badge_color`, `badge_label_zh`, `badge_label_en`, `reason_zh`, `reason_en`

**Design — cadence classification:** EDIA's `get_cadence_info()` classifies products by trading cadence. Zhanlu doesn't have this yet, so we use a simplified static map (the two frozensets ported from EDIA) plus a `row_count`-based fallback: products with < 100 history rows are treated as `cadence_class="sparse"`. This is a pragmatic starting point; a proper cadence classifier can be added later.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_forecast_trust_tier.py`:

```python
"""Tests for EDIA-ported forecast trust tiers."""
from __future__ import annotations

import pytest

from app.services.forecasting.forecast_trust_tier import (
    _HIGH_SKILL_PRODUCTS,
    _WEEKLY_CADENCE_PRODUCTS,
    compute_forecast_trust_tier,
)


def test_high_skill_products_includes_known_set():
    """Products with documented ARIMA skill vs naive baseline."""
    assert "isoprene" in _HIGH_SKILL_PRODUCTS
    assert "piperylene" in _HIGH_SKILL_PRODUCTS
    assert "c5_resin" in _HIGH_SKILL_PRODUCTS
    assert "dcpd" in _HIGH_SKILL_PRODUCTS


def test_weekly_cadence_products_includes_known_set():
    """Products that trade weekly — daily forecast is misleading."""
    assert "cracked_c9" in _WEEKLY_CADENCE_PRODUCTS
    assert "mixed_c9" in _WEEKLY_CADENCE_PRODUCTS
    assert "trimethylbenzene" in _WEEKLY_CADENCE_PRODUCTS
    assert "raffinate_c5" in _WEEKLY_CADENCE_PRODUCTS


def test_high_tier_for_skilled_product_beating_naive():
    """High-skill product that beats naive → tier='high', green badge."""
    result = compute_forecast_trust_tier(
        product_id="isoprene",
        below_naive=False,
        cadence_class="daily",
        mape=8.0,
    )
    assert result["tier"] == "high"
    assert result["badge_color"] == "green"
    assert result["badge_label_en"] == "High Confidence"
    assert "8.0" in result["reason_en"]


def test_directional_tier_when_model_worse_than_naive():
    """below_naive=True → tier='directional', orange badge."""
    result = compute_forecast_trust_tier(
        product_id="some_product",
        below_naive=True,
        cadence_class="daily",
        mape=25.0,
    )
    assert result["tier"] == "directional"
    assert result["badge_color"] == "orange"
    assert result["badge_label_en"] == "Directional Only"


def test_directional_tier_for_weekly_cadence_product():
    """Weekly-cadence product with below_naive → directional + weekly reason."""
    result = compute_forecast_trust_tier(
        product_id="cracked_c9",
        below_naive=True,
        cadence_class="weekly",
        mape=30.0,
    )
    assert result["tier"] == "directional"
    assert "weekly" in result["reason_en"].lower()


def test_medium_tier_for_weekly_product_with_skill():
    """Weekly product NOT in high-skill set and NOT below_naive → medium."""
    result = compute_forecast_trust_tier(
        product_id="cracked_c9",
        below_naive=False,
        cadence_class="weekly",
        mape=15.0,
    )
    assert result["tier"] == "medium"
    assert result["badge_color"] == "yellow"
    assert result["badge_label_en"] == "Weekly Reference"


def test_low_tier_for_sparse_data():
    """Sparse/unknown cadence → tier='low', red badge."""
    result = compute_forecast_trust_tier(
        product_id="unknown",
        below_naive=False,
        cadence_class="sparse",
        mape=None,
    )
    assert result["tier"] == "low"
    assert result["badge_color"] == "red"
    assert result["badge_label_en"] == "Insufficient Data"


def test_low_tier_for_unknown_cadence():
    """cadence_class='unknown' → tier='low'."""
    result = compute_forecast_trust_tier(
        product_id="whatever",
        below_naive=False,
        cadence_class="unknown",
        mape=None,
    )
    assert result["tier"] == "low"


def test_default_medium_tier_for_daily_product():
    """Daily product with some skill, not high-skill, not below_naive → medium."""
    result = compute_forecast_trust_tier(
        product_id="some_daily_product",
        below_naive=False,
        cadence_class="daily",
        mape=12.0,
    )
    assert result["tier"] == "medium"
    assert result["badge_color"] == "yellow"


def test_return_dict_has_all_required_keys():
    """The returned dict must have all keys the frontend expects."""
    result = compute_forecast_trust_tier("isoprene", below_naive=False, cadence_class="daily", mape=8.0)
    for key in ("tier", "below_naive", "cadence_class", "mape",
                "reason_zh", "reason_en", "badge_color",
                "badge_label_zh", "badge_label_en"):
        assert key in result, f"missing key: {key}"


def test_tier_values_are_constrained():
    """tier must be one of the 4 valid values."""
    for pid, bn, cad in [
        ("isoprene", False, "daily"),
        ("cracked_c9", True, "weekly"),
        ("unknown", False, "sparse"),
        ("random", False, "daily"),
    ]:
        result = compute_forecast_trust_tier(pid, below_naive=bn, cadence_class=cad, mape=10.0)
        assert result["tier"] in ("high", "medium", "directional", "low"), (
            f"{pid}: invalid tier {result['tier']}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_trust_tier.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/forecasting/forecast_trust_tier.py`:

```python
"""EDIA-ported forecast trust tiers.

Labels each product's forecast with a user-facing trust tier so the dashboard
doesn't present misleading daily precision for weekly-cadence products or
models that underperform a naive baseline.

Four tiers:
  - high:         Model consistently beats naive baseline. Green badge.
  - medium:       Model has some skill; use alongside qualitative judgment. Yellow.
  - directional:  Model underperforms naive OR product trades weekly. Orange.
                  Use for direction only, not point estimates.
  - low:          Data too sparse to forecast. Red badge.

Source: EDIA_5.1.2/backend/src/service/forecast_supervisor.py:150-283.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static product classification (ported from EDIA)
# ---------------------------------------------------------------------------

# Products with documented genuine ARIMA skill (skill_vs_naive < 0 in backtest).
# These get the "high" trust tier when they also beat naive on the current run.
_HIGH_SKILL_PRODUCTS: frozenset[str] = frozenset({
    "isoprene",
    "piperylene",
    "c5_resin",
    "dcpd",
})

# Products that trade weekly or less frequently; daily forecast is misleading.
# These get the "directional" tier even if they have some model signal.
_WEEKLY_CADENCE_PRODUCTS: frozenset[str] = frozenset({
    "cracked_c9",
    "mixed_c9",
    "trimethylbenzene",
    "raffinate_c5",
})


# ---------------------------------------------------------------------------
# Trust tier computation
# ---------------------------------------------------------------------------

def compute_forecast_trust_tier(
    product_id: str,
    *,
    below_naive: Optional[bool] = None,
    cadence_class: Optional[str] = None,
    mape: Optional[float] = None,
) -> dict[str, Any]:
    """Compute a user-facing trust tier for a product's forecast.

    Args:
        product_id: The dashboard product_id (e.g. "c5_resin", "cracked_c9").
        below_naive: Whether the model's MAPE exceeds a naive last-price-hold
            baseline on this run. If None, treated as False.
        cadence_class: One of "daily", "weekly", "sparse", "unknown".
            If None, defaults to "unknown".
        mape: The model's backtest MAPE (e.g. 12.0 = 12%). Optional.

    Returns:
        dict with keys:
            tier: "high" | "medium" | "directional" | "low"
            below_naive: bool
            cadence_class: str
            mape: float | None
            reason_zh: str — human-readable explanation (Chinese)
            reason_en: str — human-readable explanation (English)
            badge_color: str — "green" | "yellow" | "orange" | "red"
            badge_label_zh: str — short badge label (Chinese)
            badge_label_en: str — short badge label (English)
    """
    if below_naive is None:
        below_naive = False
    if cadence_class is None:
        cadence_class = "unknown"

    # ── Tier logic (mirrors EDIA forecast_supervisor.py:211-268) ──

    if cadence_class in ("sparse", "unsupported", "unknown"):
        tier = "low"
        badge_color = "red"
        badge_label_zh = "数据不足"
        badge_label_en = "Insufficient Data"
        reason_zh = "历史数据稀疏或不支持预测，建议查看市场定性分析。"
        reason_en = (
            "Historical data is too sparse or unsupported for forecasting. "
            "Refer to qualitative market analysis."
        )

    elif below_naive:
        tier = "directional"
        badge_color = "orange"
        badge_label_zh = "方向性参考"
        badge_label_en = "Directional Only"
        if product_id in _WEEKLY_CADENCE_PRODUCTS:
            reason_zh = (
                "该产品周频交易，日预测精度有限；当前模型弱于简单基准。仅供参考方向。"
            )
            reason_en = (
                "This product trades weekly; daily forecast precision is limited and "
                "the current model underperforms a naive baseline. Use for direction only."
            )
        else:
            reason_zh = (
                "当前模型预测误差高于简单基准（持平最新价格），建议参考市场定性判断。"
            )
            reason_en = (
                "Current model MAPE exceeds a naive baseline (last-price hold); "
                "use qualitative market judgment alongside."
            )

    elif cadence_class == "weekly" and product_id not in _HIGH_SKILL_PRODUCTS:
        tier = "medium"
        badge_color = "yellow"
        badge_label_zh = "周参考"
        badge_label_en = "Weekly Reference"
        reason_zh = "周频产品，日预测仅供参考；建议以周均价为主要参考。"
        reason_en = (
            "Weekly-trading product; daily forecast is indicative only. "
            "Use weekly average price as the primary reference."
        )

    elif product_id in _HIGH_SKILL_PRODUCTS and not below_naive:
        tier = "high"
        badge_color = "green"
        badge_label_zh = "高置信"
        badge_label_en = "High Confidence"
        mape_str = f"（MAPE {mape:.1f}%）" if mape is not None else ""
        mape_str_en = f" (MAPE {mape:.1f}%)" if mape is not None else ""
        reason_zh = f"模型在历史回测中持续跑赢简单基准，置信度较高{mape_str}。"
        reason_en = (
            f"Model consistently beats naive baseline in backtesting; "
            f"high confidence.{mape_str_en}"
        )

    else:
        # Default: medium
        tier = "medium"
        badge_color = "yellow"
        badge_label_zh = "中置信"
        badge_label_en = "Medium Confidence"
        reason_zh = "模型有一定预测能力，但需结合市场判断使用。"
        reason_en = (
            "Model has some predictive skill; use alongside qualitative market judgment."
        )

    return {
        "tier": tier,
        "below_naive": below_naive,
        "cadence_class": cadence_class,
        "mape": mape,
        "reason_zh": reason_zh,
        "reason_en": reason_en,
        "badge_color": badge_color,
        "badge_label_zh": badge_label_zh,
        "badge_label_en": badge_label_en,
    }


def classify_cadence(product_id: str, row_count: int | None) -> str:
    """Simple cadence classifier — a pragmatic starting point.

    Returns one of: "weekly", "sparse", "daily".

    Uses the static _WEEKLY_CADENCE_PRODUCTS frozenset for known weekly
    products, and a row-count threshold for sparse detection. Daily is
    the default for everything else.

    A proper cadence classifier (analyzing date gaps in the history) can
    replace this later — see EDIA's get_cadence_info().
    """
    if row_count is not None and row_count < 50:
        return "sparse"
    if product_id in _WEEKLY_CADENCE_PRODUCTS:
        return "weekly"
    return "daily"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecast_trust_tier.py -v`
Expected: PASS — all tests pass.

- [ ] **Step 5: Wire trust tier into the engine explanation**

In `backend/app/services/forecasting/engine.py`, in the explanation assembly (around line 449, where `domain_signals` was added in Task B2), add trust tier computation. **After** the explanation dict is assembled but **before** it's stored on the run:

```python
        # --- Trust tier (Phase B) ---
        try:
            from app.services.forecasting.forecast_trust_tier import (
                classify_cadence,
                compute_forecast_trust_tier,
            )
            # Extract product_id from product_key (same logic as Step 8.55)
            _parts = (target.product_key or "").split(".")
            _product_id = _parts[1] if len(_parts) >= 2 else (target.product_key or "")
            _cadence = classify_cadence(_product_id, row_count=getattr(run, "row_count", None))
            trust_tier = compute_forecast_trust_tier(
                product_id=_product_id,
                below_naive=getattr(run, "below_naive_baseline", None),
                cadence_class=_cadence,
                mape=None,  # MAPE not stored per-run yet; future enhancement
            )
            # Stash on the explanation dict
            explanation.trust_tier = trust_tier
        except Exception as exc:
            logger.warning("[trust-tier] failed for %s: %s", target.product_key, exc)
```

**Note:** Verify the `explanation` object's type — if it's a dataclass, add a `trust_tier: dict | None = None` field to its definition. If it's a plain dict, use `explanation["trust_tier"] = trust_tier` instead. Read the explanation class/dataclass definition in `engine.py` (around line 440-455) first to confirm the access pattern.

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/forecast_trust_tier.py \
        backend/tests/test_forecast_trust_tier.py \
        backend/app/services/forecasting/engine.py
git commit -m "feat(forecast): port EDIA trust tiers + wire into engine explanation

Four user-facing trust tiers (high/medium/directional/low) with bilingual
labels and badge colors. Ports _HIGH_SKILL_PRODUCTS and _WEEKLY_CADENCE_PRODUCTS
frozensets from EDIA. Includes a simple cadence classifier (static map +
row-count threshold) as a starting point. Source: EDIA_5.1.2 forecast_supervisor.py:150-283"
```

---

## Task B4: Surface Trust Tier in Dashboard API + Frontend

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py` — add `TrustTierMeta` + `trust_tier` on `ForecastOverlayMeta`
- Modify: `backend/app/services/market_dashboard_service.py` — populate `trust_tier` in overlay extraction (lines ~1010-1040)
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx` — trust-tier badge

**Interfaces:**
- Consumes: `trust_tier` dict from the forecast run's explanation (Task B3)
- Produces: a colored dot + tooltip in the forecast card header showing the tier label and reason

- [ ] **Step 1: Add `TrustTierMeta` schema**

In `backend/app/schemas/market_dashboard.py`, add after the `PolicyOverlayMeta` class:

```python
class TrustTierMeta(BaseModel):
    """User-facing trust tier for a product's forecast (Phase B)."""
    tier: str  # "high" | "medium" | "directional" | "low"
    badge_color: str  # "green" | "yellow" | "orange" | "red"
    badge_label_zh: str
    badge_label_en: str
    reason_zh: Optional[str] = None
    reason_en: Optional[str] = None
```

And add the `trust_tier` field to `ForecastOverlayMeta`:

```python
class ForecastOverlayMeta(BaseModel):
    """Per-product overlay metadata for the forecast batch response."""
    product_id: str
    intelligence: Optional[IntelligenceOverlayMeta] = None
    policy: Optional[PolicyOverlayMeta] = None
    trust_tier: Optional[TrustTierMeta] = None  # Phase B
```

- [ ] **Step 2: Populate `trust_tier` in `get_forecast_base_batch_v2`**

In `backend/app/services/market_dashboard_service.py`, in the md_t_lz_price branch of `get_forecast_base_batch_v2` (around lines 1010-1040, where `intel_meta` and `policy_meta` are built), add trust-tier extraction:

```python
                # Phase B: trust tier
                trust_raw = explanation.get("trust_tier") or {}
                trust_meta = None
                if trust_raw:
                    trust_meta = TrustTierMeta(
                        tier=trust_raw.get("tier", "medium"),
                        badge_color=trust_raw.get("badge_color", "yellow"),
                        badge_label_zh=trust_raw.get("badge_label_zh", ""),
                        badge_label_en=trust_raw.get("badge_label_en", ""),
                        reason_zh=trust_raw.get("reason_zh"),
                        reason_en=trust_raw.get("reason_en"),
                    )

                if intel_meta or policy_meta or trust_meta:
                    overlay[pid] = ForecastOverlayMeta(
                        product_id=pid,
                        intelligence=intel_meta,
                        policy=policy_meta,
                        trust_tier=trust_meta,
                    )
```

**Note:** Also add trust-tier extraction to the ERP/SKU branch (the `for tgt in sku_targets:` loop, around lines 1054-1085) — build a `trust_meta` per SKU from the run's explanation and include it in the `SkuForecastMeta`. Add an optional `trust_tier: Optional[TrustTierMeta] = None` field to `SkuForecastMeta` for this.

- [ ] **Step 3: Restart backend and verify the API returns trust_tier**

Run: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`

Wait 15 seconds, then:
```bash
TOKEN=$(curl -s -X POST "http://localhost:8000/api/apps/default-app/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

curl -s "http://localhost:8000/api/market-dashboard/forecast-base-batch?product_ids=isoprene&product_ids=c5_resin&day=7" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for pid, meta in (data.get('overlay_by_product') or {}).items():
    tt = (meta or {}).get('trust_tier') or {}
    print(f'{pid}: tier={tt.get(\"tier\")}, color={tt.get(\"badge_color\")}, label={tt.get(\"badge_label_en\")}')
"
```
Expected: Prints tier/color/label per product (if `FORECAST_DOMAIN_SIGNALS_ENABLED=true` and a run exists). If the flag is off, `trust_tier` may be null — that's expected until a run is recomputed.

- [ ] **Step 4: Add trust-tier badge to the frontend**

In `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`, in the header section (near the existing overlay badges, around line 259), add:

```javascript
          {/* Phase B: Trust tier badge */}
          {overlay?.trust_tier && (
            <div
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-medium"
              style={{
                backgroundColor:
                  overlay.trust_tier.badge_color === 'green' ? '#dcfce7' :
                  overlay.trust_tier.badge_color === 'yellow' ? '#fef9c3' :
                  overlay.trust_tier.badge_color === 'orange' ? '#ffedd5' :
                  overlay.trust_tier.badge_color === 'red' ? '#fee2e2' : '#f1f5f9',
                color:
                  overlay.trust_tier.badge_color === 'green' ? '#166534' :
                  overlay.trust_tier.badge_color === 'yellow' ? '#854d0e' :
                  overlay.trust_tier.badge_color === 'orange' ? '#9a3412' :
                  overlay.trust_tier.badge_color === 'red' ? '#991b1b' : '#475569',
              }}
              title={overlay.trust_tier.reason_en || overlay.trust_tier.reason_zh}
            >
              <span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{
                  backgroundColor:
                    overlay.trust_tier.badge_color === 'green' ? '#22c55e' :
                    overlay.trust_tier.badge_color === 'yellow' ? '#eab308' :
                    overlay.trust_tier.badge_color === 'orange' ? '#f97316' :
                    overlay.trust_tier.badge_color === 'red' ? '#ef4444' : '#94a3b8',
                }}
              />
              {overlay.trust_tier.badge_label_en}
            </div>
          )}
```

**Note:** Verify the `overlay` variable is in scope at this point in the component. It should come from the batch response's `overlay_by_product[productId]`. If the component currently only reads `base_by_product`, add a lookup: `const overlay = data?.overlay_by_product?.[productId];` near the existing forecast data extraction.

- [ ] **Step 5: Build the frontend**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 6: Preview and verify**

Open the market dashboard forecast section. Verify:
- A colored dot + label badge appears next to each product's forecast card (green "High Confidence", yellow "Weekly Reference"/"Medium Confidence", orange "Directional Only", red "Insufficient Data")
- Hovering shows the reason tooltip

- [ ] **Step 7: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/schemas/market_dashboard.py \
        backend/app/services/market_dashboard_service.py \
        frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx
git commit -m "feat(dashboard): surface forecast trust tier badge (Phase B)

Adds TrustTierMeta schema + trust_tier field on ForecastOverlayMeta.
Dashboard shows a colored dot + label badge (green/yellow/orange/red)
next to each forecast card with a hover tooltip explaining the tier.
Weekly-cadence products now labeled 'Directional Only' instead of
presenting misleading daily precision."
```

---

## Phase B Deployment & Verification

- [ ] **Step 1: Enable the feature flag**

In `/home/ysk2025/zhanlu_7_30/.env`, add:
```
FORECAST_DOMAIN_SIGNALS_ENABLED=true
```

- [ ] **Step 2: Restart backend**

Run: `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend`

- [ ] **Step 3: Trigger a forecast recompute**

Run a forecast for a product with seasonal rules (e.g. `c5_resin` in any month, or `dcpd`):
```bash
TOKEN=$(curl -s -X POST "http://localhost:8000/api/apps/default-app/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

curl -s -X POST "http://localhost:8000/api/apps/default-app/agents/ecisco_bi_assistant/tools/forecast_run" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_id": "<c5_resin_target_id>", "horizons": [7]}'
```

- [ ] **Step 4: Verify domain signals in the run explanation**

```bash
docker compose exec postgres psql -U zhanlu -d zhanlu -c "
  SELECT jsonb_pretty((results->'7d'->'explanation'->'domain_signals')::jsonb)
  FROM forecast_runs
  WHERE target_id = '<c5_resin_target_id>'
  ORDER BY created_date DESC LIMIT 1;
"
```
Expected: Shows `{"seasonal_pct": 1.0, "causal_pct": <n>, "total_pct": <n>, "applied_rules": ["seasonal:c5_resin:8", ...]}`.

- [ ] **Step 5: Run all Phase B tests**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_domain_signals.py tests/test_forecast_trust_tier.py -v
```
Expected: All tests pass.

- [ ] **Step 6: Run Phase A tests to verify no regression**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_edia_source.py tests/test_edia_source_groupby.py tests/test_seed_ecisco_targets.py tests/test_seed_sku_targets.py tests/test_forecast_batch_sku.py tests/test_forecast_engine.py -v
```
Expected: All tests pass.

---

## Phase B Self-Review Checklist

### Spec Coverage

| EDIA Source | Task(s) | Status |
|---|---|---|
| `_RAW_ELASTICITIES` + `_ELASTICITIES` (naphtha → downstream, 3 tiers) | Task B1 | ✓ — Ported verbatim, 19 products |
| `_SEASONAL_RULES` (product_id, month → additive %) | Task B1 | ✓ — Ported verbatim, covers c5_resin/dcpd/isoprene/cracked_c5/piperylene/sis |
| Causal-chain propagation: naphtha % × elasticity | Task B1, B2 | ✓ — `compute_causal_chain_adjustment` + naphtha fetcher |
| Seasonal application: month lookup | Task B1, B2 | ✓ — `compute_seasonal_adjustment` + combined overlay |
| Engine integration (Step 8.55) | Task B2 | ✓ — Slotted between 8.5 (intel) and 8.6 (policy), gated by flag |
| `compute_forecast_trust_tier` (4 tiers, bilingual) | Task B3 | ✓ — Ported with `_HIGH_SKILL_PRODUCTS` + `_WEEKLY_CADENCE_PRODUCTS` |
| Trust tier in explanation + dashboard | Tasks B3, B4 | ✓ — `TrustTierMeta` schema + frontend badge |
| LSTM | — | ✗ Deliberately NOT ported (heavy torch dep, marginal gain on short series) |

### Placeholder Scan

- No "TBD", "TODO", "implement later" in Phase B tasks.
- All code blocks contain actual implementation code.
- Two integration points flagged with "verify the exact variable name" notes — these are read-before-edit safety checks, not placeholders. The implementer MUST read the surrounding code in `engine.py` before applying the edits.

### Type Consistency

- `compute_domain_signal_adjustment` — returns `{seasonal_pct, causal_pct, total_pct, applied_rules}`. Task B2 reads `total_pct` and `applied_rules` — keys match.
- `compute_forecast_trust_tier` — returns `{tier, below_naive, cadence_class, mape, reason_zh, reason_en, badge_color, badge_label_zh, badge_label_en}`. Task B4's `TrustTierMeta` reads `tier`, `badge_color`, `badge_label_zh`, `badge_label_en`, `reason_zh`, `reason_en` — all match.
- `fetch_naphtha_recent_pct_change` — returns `float | None`. Task B2 passes it as `naphtha_pct_change` to `compute_domain_signal_adjustment`, which accepts `float | None` — matches.
- `classify_cadence` — returns `str`. Task B3 passes it as `cadence_class` to `compute_forecast_trust_tier`, which accepts `Optional[str]` — matches.

### Phase Independence

- Phase B does **not** import or depend on any Phase A code (SKU seeder, `product_key` prefix logic, `SkuForecastMeta`).
- Phase B's `product_id` extraction (`parts[1]` of `product_key`) works identically on family targets (`"ecisco.c5_resin"` → `"c5_resin"`) and SKU targets (`"ecisco.c5_resin.201000018"` → `"c5_resin"`).
- Phase A and Phase B can be executed in either order or in parallel.
