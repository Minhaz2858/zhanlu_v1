# Pack Target Decision (2026-07-31)

## TL;DR

**Outcome: ESCALATE — pack schema is incompatible with the warehouse.** The warehouse is reachable and has rich data, but the `acme_sample_v1` pack was designed for Acme's `monthly_prices` schema and cannot be made to work with the warehouse's actual schema by a simple table repoint. **The plan needs revision before any of Tasks 2-8 can ship.**

## What was diagnosed

The MySQL warehouse at `10.10.10.49:3306` (KB id `650a2505-...` for `demo_warehouse_prod`) is **reachable** from the docker network. It contains 139 tables/views with rich data, including time-series prices for many sample products.

### Key table findings

| Table | Rows | Date col | Date range | Notes |
|---|---|---|---|---|
| `monthly_prices` | — | — | — | **Confirmed: does not exist (1146).** |
| `actual_price` | 2,340 | `date` | 2025-05-01 → 2025-07-31 | Normal English column names. 9 distinct `material_id` (A001, A002, A004-A006, A008-A010, A012). No `product` column. |
| `v_lz_data` | 10,872 | `biz_date(业务日期)` | 2025-03-07 → 2026-03-20 | **Column names have parens** (`material_name(产品名称)`, `tax_price(含税单价)`). Contains LZ001 Lanzhou prices. Sample row: `分解sample / 2025-06-13 / 4300`. |
| `v_market_data` | 10,476 | `biz_date` | 2025-03-07 → 2026-03-20 | Similar structure to v_lz_data. |
| `v_forecast_price` | 672 | `forecast_date` | 2025-05-26 → 2025-06-06 | Acme forecast results (read-only history). |
| `v_partner_forecast_price` | 610 | `date` | 2025-05-01 → 2025-05-30 | Partner forecasts. |
| `market_prices` | 10 | `date` | 2026-07-23 → 2026-07-30 | Only 10 rows — too thin. |
| `v_sis_data` | 85 | `biz_date(业务日期)` | 2025-06-15 → 2026-07-29 | One product view (SIS). |
| `v_电子元件_data` | 1,443 | `biz_date(业务日期)` | 2025-03-07 → 2026-07-29 | Product_D. |
| `v_材料J_data` | 1,007 | `biz_date(业务日期)` | 2025-03-07 → 2026-07-29 | Styrene. |
| `v_demo_data` | 1,862 | `biz_date(业务日期)` | 2025-06-13 → 2026-03-20 | Cracked sample product. |
| `v_材料K_data` | 999 | `biz_date(业务日期)` | 2025-06-15 → 2026-03-20 | Cracked sample product. |
| (and other `v_材料B_data`, `v_包装材料_data`, `v_材料H_data`, etc.) | | | | |

## Why the simple "repoint" plan doesn't work

The forecast engine (`app/services/forecasting/engine.py:_fetch_series` at line 361) reads `target.datasource` with this schema:

```json
{
  "table": "<table_name>",
  "time_column": "<date_col>",          // NOT "time_col"
  "measure": "<price_col>",
  "dimensions": ["<col>"]               // LIST, not dict
}
```

The pack (`acme_sample_v1.json`) defines products with a **different schema**:

```json
"source_hints": {
  "table": "monthly_prices",
  "time_col": "date",        // wrong key — engine uses "time_column"
  "measure": "price",
  "dims": {                   // wrong key — engine uses "dimensions" (list)
    "product": "cracked_sample"   // not a list
  },
  "granularity": "monthly",
  "region": "east_china"
}
```

Even if we repoint `table: "monthly_prices"` → `table: "actual_price"` (or any real table), there are **three blockers**:

### Blocker 1 — Schema key mismatch

Pack uses `time_col` and `dims` (dict). Engine uses `time_column` and `dimensions` (list). The engine would skip the pack's datasource config (returning `None`) because `ds.get("time_column")` would be `None`.

**Required:** Either translate pack schema → engine schema in the tool handler, OR rewrite the pack to match engine schema.

### Blocker 2 — `actual_price` has no product column

`actual_price` has `material_id` (`A001`, `A002`, etc.) and `partner_name` — no `product` column. The pack's `dims.product = "cracked_sample"` doesn't match any value in the table.

**Required:** A `material_id` lookup (e.g. `cracked_sample → A001`), which doesn't exist.

### Blocker 3 — `v_lz_data` has Chinese material names

`v_lz_data.material_name(产品名称)` contains values like `'分解sample'`, `'电子元件'`, `'材料J'`. The pack's `dims.product = "cracked_sample"` doesn't match these Chinese names.

**Required:** A `product_key → Chinese name` mapping. ~9 of 16 products have a direct LZ view (`v_demo_data` ↔ `cracked_sample`, etc.), but 7 products (product_c, feedstock_a, product_a, hydrogenated_market, product_e, butadiene, sbs) have no corresponding LZ view.

## Possible paths forward

These are options the user should choose from. None of them is a simple "fix the plan, ship it":

### Option 1: Per-product table repoint + name mapping (medium effort, partial coverage)

- Rewrite the pack to use the engine's schema (rename `time_col → time_column`, restructure `dims` dict → `dimensions` list).
- Add a `name_mapping` field per product (e.g. `cracked_sample → 分解sample`).
- Repoint each product to its corresponding `v_*_data` view (or `v_lz_data` with the right name filter).
- 9/16 products get data; 7/16 (`product_c`, `feedstock_a`, `product_a`, `hydrogenated_market`, `product_e`, `butadiene`, `sbs`) return `insufficient_data`.

**Effort:** ~2-3 days. **Result:** partial product coverage, the truth_gate shows `insufficient_data` for missing products.

### Option 2: Repoint all to `v_lz_data` (small effort, low coverage)

- Repoint all 16 products to `v_lz_data` with appropriate name filters.
- Most products return `insufficient_data` because they have no rows in v_lz_data.
- Only LZ001 products work.

**Effort:** ~1 day. **Result:** bad — most products have no data, looks like the same bug as before.

### Option 3: Per-product repoint to LZ views + ERPNext (large effort, full coverage)

- For each of 16 products, identify the right table (LZ view if exists, else `actual_price` if a `material_id` mapping can be found, else `insufficient_data`).
- Build a comprehensive product → table mapping in the pack.
- May require backfilling missing products from ERPNext (`erp_*` tables).

**Effort:** ~1 week. **Result:** full product coverage if the data exists, otherwise documented `insufficient_data` per product.

### Option 4: Load Acme CSV into the warehouse (different scope)

- Run `migrate_edia_to_zhanlu.py` to load `Acme_5.1.2/backend/data/forecast_training_dataset.csv` into a new `monthly_prices` table.
- Repoint pack to `monthly_prices` (which would then exist).
- Brings 16 products × years of historical data.

**Effort:** ~1-2 days. **Result:** full data coverage, the pack works as designed, but requires loading data the user has not authorized yet.

## Recommendation

**Option 1 (per-product repoint + name mapping)** is the smallest change that produces a meaningful improvement. It doesn't bring new data, but it does connect the existing 9 products' data to the forecast engine, and the truth_gate will honestly report `insufficient_data` for the 7 products without data.

The plan needs to be revised to:
1. Add a "pack schema translation" step (Task 2')
2. Add a "per-product name mapping" step (Task 2'')
3. Accept that 7/16 products will return `insufficient_data` even after the fix
4. Update Task 7's acceptance test to verify partial coverage works correctly

## Stats

- ForecastTarget rows in zhanlu DB: not directly checked (would need a postgres query)
- ForecastRun rows in zhanlu DB: not directly checked
- Pack products that can be served: 9/16 (after schema fix)
- Pack products without warehouse data: 7/16 (will return `insufficient_data`)

## How to continue

This decision record lives at:
`/home/ysk2025/zhanlu_7_30/.worktrees/forecasting/docs/superpowers/notes/2026-07-31-pack-target-decision.md`

The user must choose one of Options 1-4 before any of Tasks 2-8 can proceed. The plan and spec are otherwise correct.
