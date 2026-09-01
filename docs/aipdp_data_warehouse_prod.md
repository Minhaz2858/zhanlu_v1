# Ecisco Data Warehouse — Knowledge Graph

> **Database:** `aipdp_data_warehouse_prod` @ `10.10.10.49:3306` (MySQL 8, InnoDB)
> **Scope:** 99 base tables + 40 views = 139 objects · ~575 MB
> **Business:** Ecisco petrochemical ERP — C5/C9 value chain (惠州伊斯科 + 广东伊斯科)
> **Discovered:** 2026-08-25 (live query)

---

## 1. Connection (agent-ready snippet)

```python
import pymysql
conn = pymysql.connect(
    host="10.10.10.49", port=3306, user="root",
    password="0Gg.B7c2@tcX_jne6FMh",   # %40 in URL decodes to @
    database="aipdp_data_warehouse_prod",
    charset="utf8mb4", connect_timeout=15, read_timeout=60
)
```

URL form: `mysql+pymysql://root:0Gg.B7c2%40tcX_jne6FMh@10.10.10.49:3306/aipdp_data_warehouse_prod?charset=utf8mb4`

---

## 2. How an agent should use this graph

1. **Identify the question's domain** (sales / contract / inventory / market price / forecast / production / AI-intelligence) using the routing map in §3.
2. **Prefer business VIEWS over raw `erp_t_*` tables** — views already resolve material names, customer names, and org names (see §5).
3. **Look up the exact column names** for that view in §15 before writing SQL.
4. **Always add a date filter** (e.g. `PLANDATE >= '2025-01-01'`) on `erp_v_sale_orderentry` and other heavy views — unfiltered aggregates time out.
5. **Filter `FDOCUMENTSTATUS = 'C'`** for confirmed/real data (C = confirmed/closed).
6. **Never treat AI tables as raw data** — `decision_log`, `forecast_decision_points`, `intelligence_events` are AI-generated text, not transactional facts.
7. **Backtick-quote Chinese identifiers** and `name(中文)` aliases — see pitfalls §16.

---

## 3. Domain → Table Routing Map

| If the agent needs… | Use this table/view | Key columns |
|---|---|---|
| Sales orders (qty, price, customer, planned delivery) | `erp_v_sale_orderentry` | `FQTY_ORIGIN, FTAXPRICE, FAMOUNT, FALLAMOUNT, CUST_NAME, PLANDATE` |
| Sales by product (single product's full history) | `sale_erp_v_<产品>_data` (12 views, §10) | same shape as sale_orderentry |
| Sales by org — Huizhou plant | `erp_v_sale_huizhou` | `CUST_NAME, FQTY_ORIGIN, FAMOUNT` |
| Sales by org — Guangdong plant | `erp_v_sale_guangdong` | (mostly test data, 299 rows) |
| Contracts (signed, qty, price, validity) | `erp_v_contract` | `contract_fqty, CUST_NAME, material_name` |
| Contract execution / delivery against contract | `erp_v_contract_execution` | `contract_fqty, contract_amount, ORDERQTY, out_fqty, out_amount` |
| Contract financial totals | `erp_t_crm_contractfin` | `FCONTRACTAMOUNT, FCONTRACTAMOUNT_LC` |
| Current inventory snapshot | `erp_v_stk_inventory` (154 rows) | `material_name, material_grade, FBASEQTY, flot` |
| Historical inventory / stock levels | `erp_t_stk_inventory` (47,969) | `FMATERIALID, FBASEQTY, FSTOCKID` |
| Stock-in (receiving) records | `erp_t_stk_instock` + `erp_t_stk_instockentry` | `FREALQTY, FMATERIALID, FLOT_TEXT` |
| Raw material intake (monthly C5/C9) | `erp_v_raw_material_receiving` (400) | `material_name, FREALQTY, FYEAR, FMONTH` |
| Product quality grades | `erp_v_product_grade` (1,075) | `material_name, product_grade, realqty, fyear, fmonth` |
| Market prices (Longzhong 隆众 unified) | `v_lz_data` (11,117) | `` `material_name(产品名称)`, `tax_price(含税单价)`, `biz_date(业务日期)` `` |
| Market prices (single product, Longzhong) | `lz_v_<产品>_data` (12 views) | same shape as v_lz_data |
| Market prices (raw Chinese tables) | `裂解c5`, `裂解c9`, `异戊二烯`, `间戊二烯`, `双环戊二烯`, `苯乙烯`, `丁二烯 sbs`, `戊烷发泡剂` | `日期Time, 产品名, 厂商名, 价格, 单位` |
| Actual quoted prices (company's own quotes) | `v_actual_price` / `actual_price` (2,340) | `date, partner_name, price, material_name` |
| Forecast prices (AI/ML) | `v_forecast_price` / `forecast_price` (672) | `forecast_date, version, date, price, material_name` |
| Partner-specific forecast prices | `v_partner_forecast_price` / `partner_forecast_price` | `date, partner_name, type_contract, price` |
| Enriched product sales detail (contract+shipment+payment) | `erp_product_sales_details` (1,387) | `partner_name, contract_quantity, shipment_quantity, shipment_date, payment_date` |
| Customer master | `erp_t_bd_customer` (745) | **PK = `FCUSTID`** (not FID!), `FNAME`, `FNUMBER` |
| Supplier master | `erp_t_bd_supplier` (2,148) | `FSUPPLIERID, FNAME` |
| Material master (product catalog) | `material` (25,707) / `_ref_material_mapping` (26,538) | `material_id, material_code, material_name, material_model` |
| Material ID → code/name/unit resolution | `_ref_material_mapping` | `FMATERIALID → material_code/name/unit/product_type` |
| Lots / batches | `erp_t_bd_lotmaster` (14,216) | `FMATERIALID, FLOT` |
| Stock locations | `erp_t_bd_stock` (66) | `FSTOCKID, FNAME` |
| Units of measure | `erp_t_bd_unit_l` (67) | `FUNITID, FNAME` |
| Org structure / staff | `oa_sys_org_element` (1,733), `oa_hr_staff_person_info` (1,091), `t_org_organizations` (14) | org IDs → names |
| Market intelligence events | `intelligence_events` (6,055) | `event_type, headline, affected_commodities, direction, relevance_to_c5_c9` |
| Forecast decision points / accuracy | `forecast_decision_points` (22,476), `forecast_decision_snapshots` (801), `forecast_accuracy_log` (15,241) | AI process logs — not raw facts |

---

## 4. Entity Relationship Graph

```
                        ┌─────────────────────────────┐
                        │  erp_t_bd_customer (master) │
                        │  PK: FCUSTID  (NOT FID!)    │
                        └──────────┬──────────────────┘
                                   │ FCUSTID ← FCUSTOMERID / FCUSTID
        ┌──────────────┬───────────┼──────────────┬───────────────┐
        ▼              ▼           ▼              ▼               ▼
┌───────────────┐ ┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌──────────────────┐
│ SALES ORDER   │ │ DELIVERY   │ │ CONTRACT   │ │ OUTSTOCK    │ │ PRODUCT SALES    │
│ erp_t_sal_    │ │ NOTICE     │ │ erp_t_crm_ │ │ erp_t_sal_  │ │ DETAILS          │
│ orderentry    │ │ erp_t_sal_ │ │ contract   │ │ outstock    │ │ erp_product_     │
│               │ │ delivery-  │ │            │ │             │ │ sales_details    │
│  header→line→ │ │ notice     │ │ header→line│ │             │ │                  │
│  financial    │ │            │ │ →financial │ │             │ │                  │
└──────┬────────┘ └─────┬──────┘ └─────┬──────┘ └─────┬───────┘ └──────────────────┘
       │                │              │              │
       │   FMATERIALID (all entries point here)       │
       ▼                ▼              ▼              ▼
┌──────────────────────────────────────────────────────────────────┐
│  _ref_material_mapping  (PK: FMATERIALID)   material (master)    │
│  material_code, material_name, material_unit, product_type       │
└──────────────────────────────────────────────────────────────────┘
       ▲                          ▲
       │ FMATERIALID              │
┌──────┴──────────┐   ┌───────────┴──────────┐
│ INVENTORY       │   │ LOT / BATCH          │
│ erp_t_stk_      │   │ erp_t_bd_lotmaster   │
│ inventory       │   │ FMATERIALID, FLOT    │
│ FMATERIALID,    │   └──────────────────────┘
│ FSTOCKID, FBASEQTY
└─────────────────┘

MARKET PRICES (no FK to ERP — standalone domain)
  lz_v_<产品>_data / v_lz_data / raw Chinese tables (裂解c5 …)
  v_actual_price / v_forecast_price / partner_forecast_price
  (join to material via material_id / material_code where needed)
```

### Edge (join) reference

| From | To | Join key |
|---|---|---|
| Any ERP entry table (`*_entry`) | `_ref_material_mapping` | `FMATERIALID = FMATERIALID` |
| Any ERP header (`erp_t_sal_*`, `erp_t_crm_contract`) | `erp_t_bd_customer` | `FCUSTOMERID / FCUSTID = FCUSTID` ⚠️ not FID |
| `erp_t_sal_deliverynotice` | `erp_t_sal_deliverynoticeentry` | `FID = FID` |
| `erp_t_sal_deliverynoticeentry` | `erp_t_sal_deliverynoticeentry_f` | `FENTRYID = FENTRYID` (financial: FTAXPRICE, FAMOUNT, FALLAMOUNT) |
| `erp_t_sal_orderentry` | `erp_t_sal_orderentry_f` | `FENTRYID = FENTRYID` (financial) |
| `erp_t_crm_contract` | `erp_t_crm_contractentry` | `FID = FID` |
| `erp_t_crm_contractentry` | `erp_t_crm_contractentry_f` | `FENTRYID = FENTRYID` (financial) |
| `erp_t_crm_contract` | `erp_t_crm_contractfin` | `FID = FID` (FCONTRACTAMOUNT) |
| `erp_t_stk_instock` | `erp_t_stk_instockentry` | `FID = FID` |
| `erp_t_stk_inventory` | `material` / `_ref_material_mapping` | `FMATERIALID = FMATERIALID` |
| `erp_t_bd_lotmaster` | `_ref_material_mapping` | `FMATERIALID = FMATERIALID` |
| `actual_price` / `forecast_price` | `material` | `material_id = material_id` |
| `market_prices` | — | standalone: `product_id, date, price_type, price` (45 rows) |

**Declared FKs (14, all platform-side):** Django/auth tables, `datasets→data_sources`, `generated_reports→sessions`, `messages→sessions`, `report_deliveries→generated_reports`. ERP tables have NO declared FKs — joins are by convention only.

---

## 5. Business Views — the agent's entry points

### `erp_v_sale_orderentry` — SALES ORDERS (14,359 rows live)
Sales order lines with material + customer + org names resolved. **The primary sales table.**
`org_name, FENTRYID, FID, material_code, material_name, material_unit, material_group, material_type, material_property, material_model, FQTY_ORIGIN(数量), FDELIQTY(累计发货), FREMAINOUTQTY(剩余未出), FPRICE, FTAXRATE, FTAXPRICE, FAMOUNT(金额), FALLAMOUNT(价税合计), FTAXAMOUNT(税额), PLANDATE(计划发货日期), FBILLNO, FCUSTID, CUST_NAME`
⚠️ `instock_qty, produce_qty, plan_qty` are empty `varbinary(0)` — ignore.

### `erp_v_contract` — CONTRACTS (98,772 rows)
Contract lines. **98K rows = repeated per change/version.**
`ORG_NAME, FDATE, FNUMBER(客户编码), CUST_NAME, material_code, material_name, material_unit, FTAXRATE, FEXCHANGERATE, contract_fqty, F_PAEZ_JC`

### `erp_v_contract_execution` — CONTRACT EXECUTION (⚠️ heavy, times out on full scan)
`ORG_NAME, FID, FDATE, FVALISTARTDATE, FVALIENDDATE, FDELIVERYDATE, FBILLNO, FNAME, CUST_NAME, FHEADDELIVERYWAY, product_type, material_code, material_name, material_unit, currency, contract_fqty, contract_amount, contract_notdofqty, ORDERQTY, ORDERAMOUNT, plan_fqty, out_fqty, out_amount, out_notoutqty`

### `erp_v_stk_inventory` — CURRENT STOCK (154 rows, live snapshot, not historical)
`FOWNORGNAME, STOCKORGNAME, flot(批号), material_code, material_name, UNITNAME, material_grade, STOCKNAME, FBASEQTY`

### `erp_v_product_grade` — QUALITY GRADES (1,075 rows)
`FOWNORGNAME, STOCKORGNAME, FBILLNO, FDATE, fyear, fmonth, material_type, realqty, product_grade, material_code, material_name, unit`

### `erp_v_raw_material_receiving` — RAW MATERIAL INTAKE (400 rows, monthly)
`FYEAR, FMONTH, FOWNORGNAME, STOCKORGNAME, material_code, material_name, FREALQTY, FDATE`

### `erp_v_sale_huizhou` (14,031) / `erp_v_sale_guangdong` (299)
Same shape as `erp_v_sale_orderentry`. Huizhou = real business; Guangdong ≈ test data.

---

## 6. Raw ERP Tables (use when views are not enough)

### Sales / Delivery chain
| Table | Rows | Role |
|---|---|---|
| `erp_t_sal_orderentry` | 13,554 | Sales order lines (FQTY, FPLANDELIVERYDATE, FMATERIALID) |
| `erp_t_sal_orderentry_f` | 13,483 | Order financials (FTAXPRICE, FAMOUNT, FALLAMOUNT) |
| `erp_t_sal_orderentry_lk` | 13,056 | Order lock records |
| `erp_t_sal_orderentry_r` | 13,744 | Order return/related records |
| `erp_t_sal_orderfin` | 10,811 | Order financing (FENTRYID, FID) |
| `erp_t_sal_deliverynotice` | 84,557 | Delivery notice headers (FCUSTOMERID, FDATE, FDOCUMENTSTATUS, FBILLNO) |
| `erp_t_sal_deliverynoticeentry` | 94,033 | Delivery lines (FMATERIALID, FQTY, FDELIVERYDATE) |
| `erp_t_sal_deliverynoticeentry_f` | 94,947 | Delivery financials (FTAXPRICE, FAMOUNT, FALLAMOUNT) |
| `erp_t_sal_deliverynoticeentry_lk` | 93,430 | Delivery locks |
| `erp_t_sal_outstock` | 85,316 | Out-stock (shipment) headers |
| `erp_t_sal_outstockentry` | 93,358 | Out-stock lines (103 cols incl. FQTY, FMATERIALID) |
| `erp_t_sp_instock` / `erp_t_sp_instockentry` | 11,206 / 25,418 | Purchase/stock-in |

### Contract chain
| Table | Rows | Role |
|---|---|---|
| `erp_t_crm_contract` | 11,719 | Contract headers (FDOCUMENTSTATUS, FVALISTARTDATE, FVALIENDDATE, FCONTRACTTYPE, FCUSTID) |
| `erp_t_crm_contractentry` | 13,797 | Contract lines (FQTY, FDELIVERYDATE, F_PAEZ_DPRICE, FMATERIALID) |
| `erp_t_crm_contractentry_f` | 13,409 | Contract line financials (FTAXPRICE, FAMOUNT) |
| `erp_t_crm_contractfin` | 11,127 | Contract totals (FCONTRACTAMOUNT, FCONTRACTAMOUNT_LC) |

### Inventory chain
| Table | Rows | Role |
|---|---|---|
| `erp_t_stk_inventory` | 47,969 | Inventory balances (FMATERIALID, FSTOCKID, FBASEQTY, FBASELOCKQTY, FPRODUCEDATE, FEXPIRYDATE) |
| `erp_t_stk_instock` | 14,303 | Stock-in headers |
| `erp_t_stk_instockentry` | 41,108 | Stock-in lines (FREALQTY, FLOT_TEXT, FSRCBILLNO) |
| `erp_t_bd_lotmaster` | 14,216 | Lot/batch master |

### Master data
| Table | Rows | Notes |
|---|---|---|
| `erp_t_bd_customer` | 745 | **PK FCUSTID**, FNAME (no FSHORTNAME), FNUMBER = CUSTxxxx |
| `erp_t_bd_supplier` | 2,148 | Supplier master |
| `material` | 25,707 | Full material master (org, code, name, model, group, type, safe/max/min inventory) |
| `_ref_material_mapping` | 26,538 | **Reference bridge: FMATERIALID → code/name/unit/product_type** |
| `erp_t_bd_stock` | 66 | Stock locations |
| `erp_t_bd_unit_l` | 67 | UoM |
| `erp_t_bd_stockstatus_l` | 26 | Stock statuses (可用/优级品 etc.) |
| `erp_t_bd_fdatavalue` | 35 | Aux data values |

---

## 7. Market Price Domain

### Longzhong (隆众) market data — 3 layers
1. **Raw:** `md_t_lz_price` (2,639) — FMATERIAL_NAME, FTAXPRICE, FSOURCE, FDATE
2. **Per-product views:** `lz_v_<产品>_data` (12 views) — 裂解c5 (1,918), 异戊二烯 (1,471), 双环戊二烯, 戊烷泡发剂, 间戊二烯, 苯乙烯, 裂解c9, 异戊烷, 正戊烷, 环戊烷, 混三甲苯, 甲基四氢苯酐, sis
3. **Unified view:** `v_lz_data` (11,117) — everything together

**Column aliases use `name(中文)` format — MUST backtick-quote the full string:**
`` `material_name(产品名称)`, `supplier_name(厂商名称)`, `tax_price(含税单价)`, `biz_date(业务日期)`, `unit(计量单位)`, `data_source(数据来源)` ``
`tax_price` is VARCHAR — CAST before arithmetic.

### Raw Chinese product tables (company-scraped prices)
`裂解c5` (1,302), `裂解c9` (774), `异戊二烯` (1,036), `间戊二烯` (774), `双环戊二烯` (1,013), `苯乙烯` (774), `丁二烯 sbs` (1,030, note space), `戊烷发泡剂` (1,721)
Columns: `日期Time, 产品名, 厂商名, 价格, 单位` — suppliers like 扬子石化, 茂名石化, 上海石化, 华东市场价, 布伦特(USD/bbl), 日本石脑油(USD/ton).

### Company price & forecast
| Table/View | Rows | Notes |
|---|---|---|
| `actual_price` / `v_actual_price` | 2,340 | Own quotes to partners: date, partner_name, price (¥3,125–5,900 range, 9 materials, 14 partners) |
| `forecast_price` / `v_forecast_price` | 672 | AI forecast: forecast_date, version, date, price |
| `partner_forecast_price` / `v_partner_forecast_price` | 610 | Per-partner forecast incl. type_contract |
| `market_prices` | 45 | Small standalone: product_id, price_type, price |
| `oilchem_data` | 92 | Oilchem source |
| `md_yuyue` | 2,844 | 预约 price data |

---

## 8. Product-Specific Sales Views (`sale_erp_v_*`)

Each = `erp_v_sale_orderentry` filtered to one product. Identical 26-col shape:
`org_name, FENTRYID, FID, material_code, material_name, material_model, material_unit, material_group, material_type, material_property, FQTY_ORIGIN, FDELIQTY, FREMAINOUTQTY, FPRICE, FTAXRATE, FTAXPRICE, FAMOUNT, FALLAMOUNT, FTAXAMOUNT, PLANDATE, FBILLNO, FCUSTID, CUST_NAME`

| View | Product | Est. rows |
|---|---|---|
| `sale_erp_v_戊烷发泡剂_data` | Pentane blowing agent | 4,067 (live) |
| `sale_erp_v_碳五石油树脂_data` | C5 petroleum resin | ~3,310 |
| `sale_erp_v_混三甲苯_data` | Mixed trimethylbenzene | ~1,957 |
| `sale_erp_v_双环戊二烯_data` | DCPD | ~1,427 |
| `sale_erp_v_工业用裂解碳五_data` | Cracked C5 | ~1,028 |
| `sale_erp_v_乙烯炭黑料_data` | Ethylene carbon black | ~723 |
| `sale_erp_v_异戊二烯_data` | Isoprene | ~531 |
| `sale_erp_v_间戊二烯_data` | Piperylene | ~418 |
| `sale_erp_v_工业用裂解碳九_data` | Cracked C9 | ~197 |
| `sale_erp_v_工业己烷_data` | Industrial hexane | ~188 |
| `sale_erp_v_抽余碳五_data` | Raffinate C5 | ~134 |
| `sale_erp_v_sis_d2015_data` | SIS-D2015 | ~88 |

---

## 9. AI / Intelligence Tables (NOT raw business data)

| Table | Rows | Purpose |
|---|---|---|
| `intelligence_events` | 6,055 | Market intelligence events: event_type, headline, affected_commodities, direction, relevance_to_c5_c9 |
| `forecast_decision_points` | 22,476 | Forecast path points for EDIA snapshots |
| `forecast_decision_snapshots` | 801 | Canonical EDIA forecast snapshots |
| `forecast_accuracy_log` | 15,241 | Forecast accuracy metrics |
| `forecast_self_improvement_runs` | 126,818 | Self-improvement tracking |
| `forecast_shadow_predictions` | 8 | Shadow predictions |
| `decision_log` | 1 | ⚠️ AI-generated text — NOT raw data. Never use for analysis. |
| `alerts_log` | 319 | Alert events |
| `evidence_outcomes` | 0 | Self-learning evidence tier scoring |
| `insight_erp_cache` / `insight_market_cache` | 96 / 177 | Cached insights |
| `generated_reports` | 144 | Report archive |
| `user_memory` | 2 | Agent memory |

---

## 10. Platform / System Tables (ignore for business analysis)

`users` (16), `sessions` (2,117), `messages` (3,106), `auth_*`, `django_content_type`, `data_sources`, `datasets`, `dataset_column_mappings`, `dataset_permissions`, `llm_config`, `access_model_config`, `alert_rules`, `cockpit_definitions`, `report_*` (templates, schedules, categories, deliveries, product_catalog, image_assets, pinned), `hidden_gallery_templates`, `platform_products`, `auth_tokens` (1,170), `erp_price_recommendations`, `inventory_safety_config` (12).

---

## 11. Quick-Start Queries

```sql
-- Top customers by sales amount (confirmed orders, recent)
SELECT CUST_NAME, COUNT(*) AS entries, SUM(FQTY_ORIGIN) AS qty, SUM(FALLAMOUNT) AS amount
FROM erp_v_sale_orderentry
WHERE PLANDATE >= '2025-01-01' AND CUST_NAME IS NOT NULL AND CUST_NAME != ''
GROUP BY CUST_NAME ORDER BY amount DESC LIMIT 10;

-- Sales by product this year
SELECT material_name, SUM(FQTY_ORIGIN) AS qty, SUM(FALLAMOUNT) AS amount
FROM erp_v_sale_orderentry
WHERE PLANDATE >= '2026-01-01'
GROUP BY material_name ORDER BY amount DESC;

-- Current inventory
SELECT material_name, material_grade, flot, FBASEQTY
FROM erp_v_stk_inventory
ORDER BY FBASEQTY DESC;

-- Market price trend for a product (Longzhong)
SELECT `biz_date(业务日期)`, `supplier_name(厂商名称)`, CAST(`tax_price(含税单价)` AS DECIMAL(15,2)) AS price
FROM `lz_v_裂解c5_data`
WHERE `biz_date(业务日期)` >= '2026-01-01'
ORDER BY `biz_date(业务日期)`;

-- Contract pipeline
SELECT CUST_NAME, material_name, contract_fqty, FDATE, FVALIENDDATE
FROM erp_v_contract
WHERE FDATE >= '2026-01-01' AND contract_fqty IS NOT NULL;
```

---

## 12. Pitfalls & Performance Notes

1. **`erp_t_bd_customer` PK is `FCUSTID`, NOT `FID`.** Joining on FID silently breaks.
2. **`erp_v_sale_orderentry` aggregates time out without a date filter.** Always `WHERE PLANDATE >= '2025-01-01'`. Same for `erp_v_contract_execution` (full-scan timed out during discovery).
3. **`v_market_data` view times out / is unindexed — avoid.**
4. **`v2_erp_v_product_grade` is BROKEN** (references invalid tables). Use `erp_v_product_grade`.
5. **BOM in column names** (`\ufeffCUST_NAME`) in some raw tables — backtick-quote the exact name including BOM, or use views (views are clean).
6. **Chinese-alias columns** `` `name(中文)` `` must be quoted in full: `` `tax_price(含税单价)` ``.
7. **VARCHAR prices/dates**: `tax_price` is VARCHAR → CAST; dates mix `2026-01-02` and `2026/03/20` → normalize slashes in Python before comparing.
8. **`FDOCUMENTSTATUS = 'C'`** = confirmed. Filter for real numbers.
9. **Epoch dates** (1932/1900) appear in some rows — filter out.
10. **`erp_t_sal_outstockentry_f` does NOT exist** — only deliverynotice and orderentry have `_f` pairs.
11. **`erp_v_contract` has 98K rows** (version/change history) — dedupe or date-filter.
12. **pymysql returns Decimal** — float() before arithmetic.
13. **Empty varbinary columns** in views (`instock_qty`, `produce_qty`, `plan_qty`) — ignore.
14. **`丁二烯 sbs`** has a space in the table name — backtick it.

---

