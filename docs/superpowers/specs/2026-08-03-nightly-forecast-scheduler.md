# Nightly Forecast Scheduler — Design Spec

**Date:** 2026-08-03
**Author:** CodeBuddy (pair programming with user)
**Status:** Approved (user said "ok do it for me please")
**Scope:** Make `forecast_agent` run silently every night at 2 AM, producing 7-day forecasts for all 12 dashboard products. Forecast results auto-populate the Market Dashboard UI via existing 30s polling — zero frontend changes.

---

## 1. Goal

The user wants the Ecisco BI `forecast_agent` to **run continuously in the background** without human interaction. Specifically:

- Compute forecasts silently every night at 2 AM
- Cover all 12 dashboard products (Brent, Naphtha, Cracked C5/C9, Mixed C5, Isoprene, Piperylene, DCPD, SIS, Styrene, Blowing Agent, C5 Resin, Raffinate C5)
- Forecast results automatically appear on the `/market-dashboard/overview` page

The existing `ForecastEngine` (7-model ensemble: ARIMA, XGBoost, ETS, STL, naive, seasonal-naive, mean-reversion) is the source of truth — no new ML code.

## 2. Non-goals

- Real-time / streaming forecasts (nightly batch is the chosen cadence)
- Alerting or notification on failure (silent by design; logs are the only signal)
- Frontend changes (`ForecastCard` already polls every 30s — no work needed)
- Model retraining or hyperparameter tuning (existing defaults)
- Weekly report generation (separate `weekly_report/` module, already wired)

## 3. Current state (verified)

- **`ForecastEngine`** (`backend/app/services/forecasting/engine.py`): full pipeline — discovery → quality → models → ensemble → guard → scenarios → cache. Reads/writes `ForecastTarget` + `ForecastRun` + `ForecastAccuracyLog`.
- **`forecast_run` tool** (`backend/app/services/tool_handlers/forecast_tool.py:161-247`): calls `engine.compute_all(org_id, horizons, seasonal_period)` when `target_id` is omitted. Already gated `enabled_by_default=True`. Schema explicitly states "invoke it during nightly automation, not on every chat turn."
- **`automation_runtime_agent`** (`backend/app/services/automation_runtime.py`): hidden `AgentApp` per `(org_id, app_id)`. Already used as executor for all automation tasks. `ensure_automation_runtime_agent()` is idempotent.
- **`automation_dispatcher`**: ticks on cron schedule, spawns `AutomationExecution`, hands off to `automation_executor.py` which runs the agent loop with `skip_confirmation` support.
- **`/api/market-dashboard/forecast-base-batch`** (`backend/app/services/market_dashboard_service.py:816-853`): currently reads from EDIA's `v_forecast_price` view (frozen at 2025-06-12 — a stale backfill). Returns one price per product.
- **Frontend**: `ForecastCard` polls `/forecast-base-batch` every 30s via existing `useQuery({ staleTime: 25000 })`. Any change in backend response surfaces automatically.
- **EDIA MySQL mirror**: `md_t_lz_price` table (LIVE Longzhong/隆众网 sync, max FDATE 2026-07-30). Per-product primary series already mapped in `PRODUCT_TABLE_MAP` for 10 of 12 products. ERP fallback (`sale_erp_v_*_data`) for `raffinate_c5` and `c5_resin`.

## 4. Approach

**Approach A — Live ML via existing forecast engine** (selected)

Pre-seed 12 `ForecastTarget` rows (one per dashboard product), each pointing at its primary `md_t_lz_price` (or ERP) series. Extend `ForecastEngine.compute_target_anchored` to accept a new `source: "edia_mysql"` so it reads history via `get_mysql_engine()` instead of a KB. Schedule `forecast_run` nightly via the existing `automation_runtime_agent`. Switch the dashboard's `forecast-base-batch` to read `results["7d"]["base"]` from the latest `ForecastRun`.

Rejected alternatives:
- **B (lightweight heuristic script)**: bypasses the EDIA engine we just ported; no scenarios, no honesty gate, no accuracy logs. Doesn't satisfy "forecast_agent works continuously."
- **C (transform on `v_forecast_price`)**: that table is a frozen 2025-06-12 backfill; would still show stale data.

## 5. Components

Each component has one clear purpose, a well-defined interface, and is testable in isolation.

### 5.1 `app/services/forecasting/edia_source.py` (NEW, ~80 LOC)

**Purpose:** Read a time series from EDIA MySQL → `pandas.DataFrame`.

**Interface:**
```python
class EdiaMysqlDataSource:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine  # inject for tests; defaults to get_mysql_engine()

    def read_history(self, datasource: dict) -> pd.DataFrame:
        """
        datasource shape:
            {
                "source": "edia_mysql",
                "table": "md_t_lz_price",
                "time_col": "FDATE",
                "measure": "FTAXPRICE",
                "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
                "granularity": "day"
            }
        Returns DataFrame with columns ['ds', 'y'] where:
            ds = datetime
            y  = float (sentinels 'F7'/'NaN' dropped, then coerced)
        Raises RuntimeError if EDIA MySQL unreachable or no rows.
        """
```

**Behavior:**
- Builds parameterized SQL: `SELECT {time_col} AS ds, {measure} AS y FROM {table} WHERE {where} ORDER BY ds ASC`
- Coerces VARCHAR `FTAXPRICE` to float (drops sentinels `"F7"`, `"NaN"`, empty — same logic as `market_dashboard_service._read_md_lz_history_rows`)
- Returns DataFrame with `ds: datetime64`, `y: float64`, sorted ascending
- Raises `RuntimeError("EDIA MySQL unreachable")` if engine is None
- Raises `RuntimeError("No rows for {where}")` if query returns empty

### 5.2 `app/services/forecasting/engine.py` (EXTEND, ~30 LOC added)

**Purpose:** Add a branch to `compute_target_anchored` that uses `EdiaMysqlDataSource` when `target.datasource["source"] == "edia_mysql"`.

**Interface change:** None externally — same method signature, same return shape (`{"run": ForecastRun, "anchor_rows": ..., "source_table": ..., "sample_size": ...}` or None).

**Implementation:**
- Inside `compute_target_anchored`, before the existing KB-backed QueryService path, check `target.datasource.get("source")`:
  - If `"edia_mysql"`: import `EdiaMysqlDataSource`, call `.read_history(target.datasource)`, skip the KB lookup entirely.
  - Otherwise: existing path (unchanged).
- The downstream pipeline (quality → models → ensemble → guard → scenarios → cache) is **untouched** — it just receives a DataFrame instead of building one.

### 5.3 `app/services/forecasting/seed_ecisco_targets.py` (NEW, ~120 LOC)

**Purpose:** Idempotently register 12 `ForecastTarget` rows, one per dashboard product.

**Interface:**
```python
ECISCO_FORECAST_TARGETS: list[dict] = [
    {
        "product_key": "ecisco.crude_oil",
        "name": "Crude Oil (Brent)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-布伦特'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 1,
    },
    # ... 11 more
]

def seed_ecisco_forecast_targets(db: Session, org_id: str = "default-org", app_id: str = "default-app") -> int:
    """Insert any missing Ecisco forecast targets. Returns count of new rows."""
```

**Product → source mapping** (matches `PRODUCT_TABLE_MAP` primary config):
| product_id | source table | where clause | report_order |
|---|---|---|---|
| `crude_oil` | `md_t_lz_price` | `FMATERIAL_NAME = '裂解C5-布伦特'` | 1 |
| `naphtha` | `md_t_lz_price` | `FMATERIAL_NAME = '裂解C5-日本石脑油'` | 2 |
| `cracked_c5` | `md_t_lz_price` | `FMATERIAL_NAME = '裂解C5-裂解C5均价'` | 3 |
| `mixed_c5` | `md_t_lz_price` | `FMATERIAL_NAME = '裂解C5-扬子裂解C5'` | 4 |
| `cracked_c9` | `md_t_lz_price` | `FMATERIAL_NAME = '裂解C9-裂解C9均价'` | 5 |
| `isoprene` | `md_t_lz_price` | `FMATERIAL_NAME = '异戊二烯-华东市场价'` | 6 |
| `piperylene` | `md_t_lz_price` | `FMATERIAL_NAME = '间戊二烯-华东市场价'` | 7 |
| `dcpd` | `md_t_lz_price` | `FMATERIAL_NAME = '双环戊二烯-华东市场价'` | 8 |
| `sis` | `md_t_lz_price` | `FMATERIAL_NAME = 'SIS-巴陵1105'` | 9 |
| `styrene` | `md_t_lz_price` | `FMATERIAL_NAME = '苯乙烯-中石化华东'` | 10 |
| `blowing_agent` | `md_t_lz_price` | `FMATERIAL_NAME = '戊烷发泡剂-华东市场价'` | 11 |
| `c5_resin` | `sale_erp_v_碳五石油树脂_data` | `1=1` (no material filter; PLANDATE/FTAXPRICE) | 12 |
| `raffinate_c5` | `sale_erp_v_抽余碳五_data` | `1=1` | 13 |

**Idempotency:** `SELECT 1 FROM forecast_targets WHERE product_key = :pk AND org_id = :org AND is_deleted = false`. Skip if exists.

### 5.4 `backend/seed.py` (EXTEND, ~5 LOC added)

At the end of the existing `ensure_system_agents()` call (or wherever the seeder finishes), add:
```python
from app.services.forecasting.seed_ecisco_targets import seed_ecisco_forecast_targets
seed_ecisco_forecast_targets(db)
```
Idempotent — safe on every boot.

### 5.5 Alembic migration `037_ensure_12_ecisco_forecast_targets.py` (NEW, ~50 LOC)

For existing deployments that don't re-run `seed.py`:
- Loop over `ECISCO_FORECAST_TARGETS` (imported from `seed_ecisco_targets`)
- `INSERT INTO forecast_targets (...) VALUES (...) ON CONFLICT (product_key, org_id) DO NOTHING`
- Sets `status='active'`, `source='seed'`, `level=2` (pack-grade), `quality_grade='B'` (default; will be re-scored on first run)
- Forward-only; no downgrade (targets are data, not schema)

### 5.6 `app/services/market_dashboard_service.py` (EXTEND, ~40 LOC added)

**Purpose:** Add `get_forecast_base_batch_v2` that reads from `ForecastRun` (local Postgres) instead of `v_forecast_price` (stale EDIA view).

**New constant:**
```python
PRODUCT_FORECAST_TARGET_KEY = {
    "crude_oil":     "ecisco.crude_oil",
    "naphtha":       "ecisco.naphtha",
    "cracked_c5":    "ecisco.cracked_c5",
    # ... all 12
}
```

**New function:**
```python
def get_forecast_base_batch_v2(product_ids: list[str], day: int = 7) -> ForecastBaseBatchResponse:
    """Read 7-day base forecast from local ForecastRun table (nightly cron output).
    Falls back to None for products with no run yet."""
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        out: dict = {pid: None for pid in product_ids}
        horizon_key = str(day)
        # Map product_id → ForecastTarget.product_key
        keys = [PRODUCT_FORECAST_TARGET_KEY[p] for p in product_ids if p in PRODUCT_FORECAST_TARGET_KEY]
        # Bulk-load latest ForecastRun per target
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.in_(keys),
            ForecastTarget.is_deleted == False,
        ).all()
        for tgt in targets:
            run = db.query(ForecastRun).filter(
                ForecastRun.target_id == tgt.id,
                ForecastRun.is_deleted == False,
            ).order_by(ForecastRun.created_date.desc()).first()
            if run and run.results:
                base = (run.results.get(horizon_key) or {}).get("base")
                if base is not None:
                    # Match product_id back from key
                    for pid, pk in PRODUCT_FORECAST_TARGET_KEY.items():
                        if pk == tgt.product_key and pid in out:
                            out[pid] = float(base[0] if isinstance(base, list) else base)
                            break
        return ForecastBaseBatchResponse(day=day, base_by_product=out)
    finally:
        db.close()
```

**Router change** (`backend/app/routers/market_dashboard.py`):
- Find the existing `/forecast-base-batch` endpoint
- Replace `get_forecast_base_batch(pids, day=day)` with `get_forecast_base_batch_v2(pids, day=day)`
- Old function (`get_forecast_base_batch`) stays in place as a fallback if needed — single-line change in the router.

### 5.7 `app/routers/automation_api.py` (EXTEND, ~40 LOC added)

**Purpose:** Add admin-only endpoint to bootstrap the nightly forecast automation task.

**New endpoint:**
```python
@router.post("/bootstrap-nightly-forecast")
def bootstrap_nightly_forecast(
    db: Session = Depends(get_db),
    user = Depends(get_admin_user),  # existing admin dependency
):
    """Create the idempotent nightly forecast automation task.
    Returns the existing AutomationTask if one already exists for this (org, app)."""
    from app.services.automation_runtime import ensure_automation_runtime_agent
    from app.services.forecasting.seed_ecisco_targets import seed_ecisco_forecast_targets
    from app.models.automation_task import AutomationTask

    org_id = user.org_id
    app_id = "default-app"
    cron = settings.NIGHTLY_FORECAST_CRON  # default "0 2 * * *"

    # 1. Ensure targets exist (idempotent)
    seed_ecisco_forecast_targets(db, org_id, app_id)

    # 2. Ensure runtime agent exists (idempotent)
    runtime = ensure_automation_runtime_agent(db, org_id, app_id)

    # 3. Find or create the automation task (idempotent by name+org)
    existing = db.query(AutomationTask).filter(
        AutomationTask.name == "Nightly Ecisco BI Forecast",
        AutomationTask.org_id == org_id,
        AutomationTask.is_deleted == False,
    ).first()
    if existing:
        return {"task_id": existing.id, "status": "exists", "cron": cron}

    task = AutomationTask(
        name="Nightly Ecisco BI Forecast",
        description="Silent nightly ML forecast for all 12 Ecisco BI dashboard products. Writes to forecast_runs; dashboard reads via /forecast-base-batch.",
        org_id=org_id,
        app_id=app_id,
        agent_id=runtime.id,
        created_by_id=user.id,
        cron=cron,
        schedule_type="cron",
        skip_confirmation=True,
        skip_human_in_the_loop=True,
        output_format="json",
        prompt=(
            "Run the `forecast_run` tool with no arguments (target_id=null) "
            "to refresh forecasts for all active targets in this org. "
            "Do not chat, summarize, or output any narrative — just call the "
            "tool, report success/failure counts, and end."
        ),
        status="active",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"task_id": task.id, "status": "created", "cron": cron}
```

### 5.8 `app/config.py` (EXTEND, ~5 LOC added)

```python
# Forecast scheduler
NIGHTLY_FORECAST_ENABLED: bool = os.getenv("NIGHTLY_FORECAST_ENABLED", "true").lower() == "true"
NIGHTLY_FORECAST_CRON: str = os.getenv("NIGHTLY_FORECAST_CRON", "0 2 * * *")
```

## 6. Data flow

### Night (2:00 AM local)

1. `automation_dispatcher` ticks; finds `AutomationTask` with `cron="0 2 * * *"`, `status="active"`.
2. Spawns `AutomationExecution(status="running")`.
3. `automation_executor._run_agent_in_conversation`:
   - Resolves `automation_runtime_agent` (hidden AgentApp).
   - Injects prompt: "Run `forecast_run` tool with no arguments..."
   - Sets `skip_confirmation=True` — no human prompts.
4. Agent loop calls `forecast_run({})`.
5. `_forecast_run._do_work()`:
   - `engine = ForecastEngine(db)`
   - `runs = engine.compute_all(org_id, horizons=[3,7,30], seasonal_period=7)`
6. `compute_all` iterates 12 `ForecastTarget` rows where `status='active'`:
   - For each, `compute_target_anchored(target_id, ...)`:
     - Sees `datasource.source == "edia_mysql"` → calls `EdiaMysqlDataSource.read_history()`
     - Pulls ~90+ days of history from `md_t_lz_price` (or ERP table)
     - Runs quality scoring → 7-model pool → ensemble blend → guard → scenarios
     - Writes `ForecastRun` row with `results={"3d": {...}, "7d": {"base": X, "bull": Y, "bear": Z}, "30d": {...}}`
     - Writes `ForecastAccuracyLog` row (backtest)
7. Returns `[ForecastRun, ...]` (12 items if all succeed).
8. Agent ends → `AutomationExecution.status="completed"`, `completed_at=now()`.

### Daytime (anytime after 2 AM)

1. User opens `/market-dashboard/overview`.
2. Frontend `useQuery` polls `/api/market-dashboard/forecast-base-batch?day=7` every 30s.
3. Endpoint calls `get_forecast_base_batch_v2(product_ids, day=7)`:
   - Queries `forecast_runs` (latest per `target_id`) for the 12 mapped `ForecastTarget` rows.
   - Returns `{"day": 7, "base_by_product": {"crude_oil": 84.5, "naphtha": 826.0, "cracked_c5": 5685.0, ...}}`.
4. `ForecastCard` re-renders with new values; 30s cache means updates propagate within 30s of the next poll.

## 7. Error handling

| Failure | Handling |
|---|---|
| One product's history is empty (e.g. SIS data gap) | `EdiaMysqlDataSource.read_history` raises `RuntimeError("No rows...")`. `compute_target_anchored` catches, logs WARNING, returns None. `compute_all` skips that target, continues with the other 11. Dashboard shows `null` for that product (existing v1 behavior). |
| EDIA MySQL unreachable (network blip) | `get_mysql_engine()` returns None. `EdiaMysqlDataSource.read_history` raises `RuntimeError("EDIA MySQL unreachable")`. Same skip-and-continue path as above. All 12 fail → `compute_all` returns `[]`. Task marked completed (not failed — silent by design). Logs at ERROR. |
| Engine bug (unhandled exception in one model) | Existing try/except in ensemble blend catches per-model failures. `model_detail.failed` lists the failed model. `ForecastRun` still written with surviving models. |
| `automation_runtime_agent` missing | `ensure_automation_runtime_agent` is called at task creation time (idempotent) — agent always exists before run. |
| Dispatcher down at 2 AM | Next dispatcher tick after recovery catches the missed run if `catch_up=true` on the task (existing dispatcher behavior — confirm during implementation). |
| Schema drift (e.g. `md_t_lz_price` column rename) | `EdiaMysqlDataSource` raises on first read. All 12 fail. Logs surface the SQL error. Manual fix required. |

**No alerting.** Silent by design — the user explicitly said "without human interaction." If the user later wants alerts, that's a separate feature.

## 8. Testing

| Test file | What it verifies |
|---|---|
| `tests/test_edia_source.py` (NEW) | `EdiaMysqlDataSource.read_history` returns DataFrame with `ds`/`y` columns; drops sentinels "F7"/"NaN"; raises on unreachable; raises on empty. Uses mocked SQLAlchemy engine. |
| `tests/test_forecast_engine_edia_source.py` (NEW) | `compute_target_anchored` with `datasource.source="edia_mysql"` calls `EdiaMysqlDataSource`, writes `ForecastRun` row with populated `results` and `below_naive_baseline` flag. Mocks the data source. |
| `tests/test_seed_ecisco_targets.py` (NEW) | `seed_ecisco_forecast_targets` inserts 12 rows on first call; 0 on second call (idempotent). Verifies `product_key`, `datasource.source`, `include_in_weekly_report`. |
| `tests/test_forecast_dashboard_endpoint_v2.py` (NEW) | `get_forecast_base_batch_v2` returns `base_7d` from latest `ForecastRun` per product; returns None for products with no run; respects `day` param. |
| `tests/test_bootstrap_nightly_forecast.py` (NEW) | Endpoint creates `AutomationTask` with `cron="0 2 * * *"`, `skip_confirmation=True`, `skip_human_in_the_loop=True`. Second call returns `status="exists"`. Verifies task prompt mentions `forecast_run`. |
| Existing 99 tests | All green — no behavior change for KB-backed path; existing `forecast_run` tool unchanged. |

## 9. Operational notes

- **Config flags:**
  - `NIGHTLY_FORECAST_ENABLED` (default `true`) — gates the bootstrap endpoint
  - `NIGHTLY_FORECAST_CRON` (default `0 2 * * *`) — easy time adjustment
  - `FORECASTING_ENABLED` (existing, default true) — gates the engine itself
  - `ECISCO_BI_AGENT_ENABLED` (existing, default false) — NOT required for the nightly job (the runtime agent is independent of the chat agent)
- **Backend restart:** `docker compose restart backend` (no `--reload`).
- **Manual trigger:** `curl -X POST http://localhost:8000/api/automations/bootstrap-nightly-forecast -H "Authorization: Bearer <admin_jwt>"` (creates task if missing; does not trigger immediate run).
- **Immediate run (debug):** `curl -X POST http://localhost:8000/api/automations/{task_id}/run-now -H "Authorization: Bearer <admin_jwt>"` (existing endpoint).
- **Verify nightly output:** `SELECT product_key, fr.created_date, fr.results->'7d'->'base' AS base_7d FROM forecast_runs fr JOIN forecast_targets ft ON fr.target_id = ft.id WHERE ft.product_key LIKE 'ecisco.%' ORDER BY ft.product_key, fr.created_date DESC;`

## 10. Implementation order

1. `app/services/forecasting/edia_source.py` (new file) + `tests/test_edia_source.py`
2. Extend `app/services/forecasting/engine.py` (edia_mysql branch) + `tests/test_forecast_engine_edia_source.py`
3. `app/services/forecasting/seed_ecisco_targets.py` (new file) + `tests/test_seed_ecisco_targets.py`
4. Alembic migration `037_ensure_12_ecisco_forecast_targets.py`
5. Extend `backend/seed.py` (call seeder)
6. Extend `app/services/market_dashboard_service.py` (`get_forecast_base_batch_v2`) + `tests/test_forecast_dashboard_endpoint_v2.py`
7. Update router to call v2
8. Extend `app/routers/automation_api.py` (bootstrap endpoint) + `tests/test_bootstrap_nightly_forecast.py`
9. Extend `app/config.py` (2 new flags)
10. `docker compose restart backend`
11. Run `curl -X POST /api/automations/bootstrap-nightly-forecast` to create the task
12. Trigger immediate run to verify end-to-end
13. Verify dashboard shows fresh forecasts

## 11. Risks

- **Engine extension risk:** Adding the `edia_mysql` branch is the only change to a tested file. The branch is conditional — existing KB-backed path is untouched. Regression risk is minimal but the new branch needs its own test.
- **Compute time at 2 AM:** 12 products × ~30 sec each = ~6 min. Acceptable for nightly. If it grows beyond 30 min, consider parallelism (existing `compute_all` is serial).
- **History depth:** Engine needs ≥14 days of history (per `guard.py` guardrail). `md_t_lz_price` has 6+ months of data for most products. The 2 ERP-only products (`raffinate_c5`, `c5_resin`) may have sparser history — if guard fails, those 2 return None and dashboard shows null (acceptable; v1 behavior).
- **Dispatcher reliability:** The existing dispatcher has been running automation tasks since 2026-07-28. No known issues. The forecast task is just another `AutomationTask` row.
