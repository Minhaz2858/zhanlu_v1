# Warehouse Data Discovery Report — 2026-08-07

## Executive Summary

The customer's data warehouse (`demo_warehouse_prod` @ `10.10.10.49:3306`) contains **139 tables**. We were only reading **~5** of them. The deep probe reveals that **ALL THREE Tier 3 fundamental data feeds can be built from existing warehouse data** — no external subscriptions needed.

The procurement questionnaire is no longer blocking.

---

## Warehouse Table Inventory (139 total)

| Category | Count | Previously Used? |
|----------|-------|-----------------|
| `erp_v_*` (views) | 8 | 2 of 8 |
| `sale_erp_v_*` | 12 | qty + price only |
| `v_*` | 13 | 2 of 13 |
| `md_t_*` | 1 | Yes |
| `v_*` | 5 | 0 |
| `erp_t_*` (raw ERP) | ~25 | **0 — ALL NEW** |
| Other (app/market/intel) | ~75 | ~3 |

---

## Tier 3 Feed Analysis

### T3.1 Operating Rate — **AVAILABLE** (production throughput proxy)

| Table | Rows | Date Range | Key Data |
|-------|------|------------|----------|
| `erp_t_sp_instock` | 11,844 | 2018-06 → 2026-07 | FDATE (8 years daily), FSTOCKORGID, FBILLNO |
| `erp_t_sp_instockentry` | 25,909 | (joined via FID) | **FREALQTY** (100% populated, max 16,288 tons), FMATERIALID (82 materials), FPRICE (779 distinct) |
| `erp_v_raw_material_receiving` | 400 | 2018-05 → 2026-06 | FREALQTY, material_name (9 products), FDATE, FYEAR, FMONTH |

**How to build**: Join `erp_t_sp_instock` (header, has FDATE) with `erp_t_sp_instockentry` (line items, has FREALQTY + FMATERIALID). Group by date + material → daily production volume per product. This is a direct **production throughput** signal — when throughput drops, operating rate has dropped.

**Also**: `erp_v_raw_material_receiving` gives monthly raw material intake volumes per product (400 rows, 8 years, 9 products) — this is the feedstock side of operating rate.

### T3.2 Inventory — **AVAILABLE** (time-series + snapshot)

| Table | Rows | Date Range | Key Data |
|-------|------|------------|----------|
| `erp_t_stk_inventory` | 50,176 | FUPDATETIME: 2018-09 → 2026-07 | **FBASEQTY** (100% populated, 492 distinct, max 672,682), FMATERIALID (17,062) |
| `erp_v_stk_inventory` | 154 | Snapshot (current) | FBASEQTY (67 distinct), material_name (9), FOUTQTY, ON_road_qty, Unmove_QTY |
| `erp_t_stk_instock` | 14,334 | FDATE: 2018-05 → 2026-07 | FSUPPLIERID (763), instock movements |
| `erp_t_stk_instockentry` | 42,970 | (joined via FID) | FREALQTY (100% populated), FALLAMOUNTEXCEPTDISCOUNT (12,324 distinct) |
| `erp_t_sal_outstock` | 89,556 | FDATE: 2018-06 → 2026-07 | FCUSTOMERID, outstock movements |
| `erp_t_sal_outstockentry` | 97,509 | (joined via FID) | FREALQTY (100% populated, max 8,680), FYKSBGHPRICE, FYKSBGHTAXPRICE |

**How to build**: Two approaches:
1. **Direct**: `erp_t_stk_inventory.FBASEQTY` grouped by `FUPDATETIME` + `FMATERIALID` → inventory level time-series (50K rows, 8 years)
2. **Reconstructed**: Cumulative `erp_t_stk_instock` (in) - `erp_t_sal_outstock` (out) → net inventory change per day per material

**Current snapshot**: `erp_v_stk_inventory` (154 rows) gives current stock levels by product, warehouse, and grade — including `ON_road_qty` (in-transit) and `Unmove_QTY` (stagnant stock).

### T3.3 Import Prices — **PARTIALLY AVAILABLE** (purchase prices with supplier identification)

| Table | Rows | Date Range | Key Data |
|-------|------|------------|----------|
| `erp_t_stk_instock` | 14,334 | 2018-05 → 2026-07 | FSUPPLIERID (763 distinct) — join with supplier master |
| `erp_t_stk_instockentry` | 42,970 | (joined via FID) | FREALQTY, FALLAMOUNTEXCEPTDISCOUNT → compute unit price = amount/qty |
| `erp_t_bd_supplier` | 2,211 | — | FNAME (273 distinct supplier names), CURRENCY_NAME |
| `erp_v_sale_guangdong` | 299 | 2024-09 → 2026-07 | CUST_NAME includes **"Acme International Investments, LLC"** (export customer) |
| `erp_v_sale_huizhou` | 13,912 | 2018-06 → 2026-07 | FPRICE (986 distinct), FTAXPRICE (812), CUST_NAME |

**How to build**: Join `erp_t_stk_instock` (has FSUPPLIERID + FDATE) with `erp_t_stk_instockentry` (has FREALQTY + FALLAMOUNTEXCEPTDISCOUNT) and `erp_t_bd_supplier` (has FNAME). Compute unit_price = FALLAMOUNTEXCEPTDISCOUNT / FREALQTY. Filter by supplier name keywords or CURRENCY_NAME ≠ "人民币" to identify import transactions.

**Supplier analysis**: Only 1 explicitly international supplier found: "Acme International Investments, LLC" (which is the customer's own export arm). Most of 273 suppliers are domestic. However, major industrial producers like "中国石油天然气股份有限公司", "宁波金海晨光化学股份有限公司", "福建联合石化" are present — these are large-scale suppliers whose pricing reflects market conditions.

**Note**: The international supplier columns described in `Acme_5.1.2/erp_mysql_schema.txt` (ARLANXEO, Kraton, Ineos, etc.) do **NOT** exist in this warehouse. That schema was from Acme's design, not the actual database.

---

## BONUS Discoveries (beyond Tier 3)

### Upstream Market Data from 隆众资讯 (Oilchem)
| Table | Rows | Date Range | Columns |
|-------|------|------------|---------|
| `oilchem_data` | 74 | 2026-04 → 2026-08 | benchmark_a_crude, japan_product_c, industrial_cracking_c5, industrial_cracking_c9 |

This is **daily upstream market data** scraped from oilchem.net (隆众资讯). Currently only 4 months, but actively updated (last: 2026-08-07). This provides:
- **Benchmark A crude** — the ultimate upstream feedstock price
- **Japan product_c** — Asian benchmark for industrial feedstock
- **Cracking C5/C9** — direct feedstock for the customer's products

### Contract Data (14 years)
| Table | Rows | Date Range |
|-------|------|------------|
| `erp_v_contract` | 97,704 | 2012-09 → 2026-07 |
| `erp_v_contract_execution` | 13,692 | 2012-09 → 2026-07 |
| `erp_t_crm_contractentry` | 14,322 | FDELIVERYDATE: 2018-06 → 2026-07 |

Contract prices (`F_PAEZ_DPRICE` 128 distinct, `F_PAEZ_PRICE_WY_PLANPRICE` 185 distinct) with delivery dates — can be used as forward price indicators.

### Sales Details with Freight
| Table | Rows | Date Range | Key Data |
|-------|------|------------|----------|
| `erp_product_sales_details` | 1,387 | 2025-01 → 2025-12 | contract_price, shipment_quantity, shipment_date, partner_name, freight_cost_per_ton, tax_rate |

This is a clean, curated sales detail table with contract prices, shipment quantities, and freight costs per ton — only 1 year but very well structured.

### Additional Unused v_* Views (6 products)
We were only reading 2 of 13 `v_*` views. The 11 unused views cover: SIS, 材料B, 材料E, 包装材料, 材料F, 材料H, 材料G, 材料I, 材料J, 材料L, 材料K, 材料C.

### Additional Unused Product Tables (8 products)
Standalone product tables with 日期Time column (2025-03 to 2026-03):
`丁二烯 sbs`, `材料B`, `电子元件`, `包装材料`, `材料J`, `材料L`, `材料K`, `材料C`

### Forecast System Tables (already populated)
| Table | Rows | Date Range |
|-------|------|------------|
| `forecast_accuracy_log` | 13,488 | 2026-04 → 2026-08 |
| `forecast_decision_points` | 19,270 | 2026-05 → 2026-08 |
| `forecast_decision_snapshots` | 896 | 2026-05 → 2026-08 |
| `forecast_self_improvement_runs` | 79,836 | 2026-05 → 2026-08 |
| `intelligence_events` | 6,474 | 2026-04 → 2026-08 |

---

## Recommended Loader Implementation Plan

### Priority 1: T3.1 Production Throughput Loader
- **Source**: `erp_t_sp_instock` JOIN `erp_t_sp_instockentry` ON FID
- **Output**: Daily production volume per material
- **Material mapping**: Join `FMATERIALID` → `material` table → material_name
- **8 years of daily data** — massive improvement over current price-only forecasting

### Priority 2: T3.2 Inventory Time-Series Loader
- **Source**: `erp_t_stk_inventory` (FUPDATETIME + FMATERIALID + FBASEQTY)
- **Output**: Inventory level per material per update timestamp
- **Supplement**: `erp_v_stk_inventory` for current snapshot including ON_road_qty
- **50K rows, 8 years** — can compute inventory turnover, stockout risk

### Priority 3: T3.3 Purchase Price Loader
- **Source**: `erp_t_stk_instock` JOIN `erp_t_stk_instockentry` JOIN `erp_t_bd_supplier`
- **Output**: Purchase unit price per supplier per material per date
- **Filter**: Identify import vs domestic suppliers via CURRENCY_NAME or name keywords
- **14K transactions, 8 years** — can compute import parity, supplier price spread

### Priority 4: Upstream Market Data Loader
- **Source**: `oilchem_data`
- **Output**: Daily Benchmark A crude, Japan product_c, C5/C9 cracking prices
- **74 rows but growing daily** — critical upstream exogenous feature

### Priority 5: Sales Volume + Price Loader (enhance existing)
- **Source**: `erp_v_sale_huizhou` + `erp_v_sale_guangdong` (13,912 + 299 rows)
- **Output**: Daily sales volume (FDELIQTY) + transaction price (FPRICE) per product per customer
- **8 years** — the existing `ErpTxLoader` only reads price, not volume. This enhances it.

### Priority 6: Raw Material Receiving Loader
- **Source**: `erp_v_raw_material_receiving`
- **Output**: Monthly raw material intake volume per product
- **400 rows, 8 years, 9 products** — feedstock supply signal

---

## Data Quality Notes

1. **`produce_qty`, `plan_qty`, `instock_qty`** in `v_sale_order` and `sale_erp_v_*` tables are ALL empty (varbinary(0), 0 non-null). These were Acme-design columns that were never populated in the actual warehouse. **Do not use them.**

2. **`FPRODUCEDATE`** is empty in all entry tables. Use the header table's `FDATE` instead for time-series.

3. **Material ID mapping**: The `material` table has 28,377 rows and can map `FMATERIALID` → material name. The `_ref_material_mapping` table (26,544 rows) may also help.

4. **`erp_t_stk_inventory.FBASEQTY`** has some negative values (min -1,390) — likely returns or adjustments. Filter or handle as signed quantities.

5. **`oilchem_data`** price columns (benchmark_a_crude, japan_product_c, etc.) appear to be NULL in the sample rows — only `source_status` (JSON with URL) is populated. Need to check if prices are in the JSON or if the scraper isn't extracting them.
