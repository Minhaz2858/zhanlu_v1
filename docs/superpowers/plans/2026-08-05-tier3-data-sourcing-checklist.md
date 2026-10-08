# Tier 3 Data-Sourcing Checklist

> **Purpose:** Tier 3 (T3.1 开工率 / T3.2 port inventory / T3.3 import & competitor prices) is the **long pole** of the forecasting roadmap — the engineering is straightforward (new loaders following the F1 pattern), but the data itself does not yet exist in any system we can query. This checklist exists so data discovery starts **today**, in parallel with Waves 0–2, so feeds exist when we reach Wave 3.
>
> **Rule:** A loader can be built the instant a feed exists. No feed = no Wave 3. Filling this checklist is the single most important action for Tier 3 to ever land.

---

## How to use this document

For each of the three data domains below, answer every question. When a cell is filled, mark the domain `[READY]`, `[PARTIAL]`, or `[BLOCKED]`. A domain is `[READY]` only when all five sections (source, access, schema, cadence, cost) are answered.

Status legend:
- `[READY]` — feed identified, accessible, schema known, can build loader now
- `[PARTIAL]` — some answers, gaps remain
- `[BLOCKED]` — no source identified yet; this is where effort must go

| Domain | Status | Owner | Last updated |
|--------|--------|-------|--------------|
| T3.1 开工率 (operating rates) | `[BLOCKED]` | — | — |
| T3.2 Port inventory | `[BLOCKED]` | — | — |
| T3.3 Import / competitor prices | `[BLOCKED]` | — | — |

---

## T3.1 — 开工率 (Downstream operating rates)

**Why it matters:** Operating rate is the single biggest fundamental gap vs human analyst reports. It is the truest real-time demand signal — when downstream plants cut runs, feedstock demand softens *before* transaction volumes or prices reflect it. Human weekly reports lead with 开工率 for a reason.

**Target products (downstream of our forecast set):**
- SIS/SBS elastomer producers (consume product_d, product_f)
- Petroleum-resin producers (consume sample product resin, PRODUCT_B)
- Styrene-butadiene / EPS / PS producers (consume styrene)
- Blowing-agent / sample product downstream producers

### Questions to answer

**1. Source identification**
- [ ] Which data provider publishes 开工率 for these downstream sectors? Candidates:
  - 隆众资讯 (Longzhong) — weekly operating-rate reports per product
  - 卓创资讯 (SCI99) — weekly 开工率 surveys
  - 安迅思 (ICIS China)
  - Wind / iFinD (聚合 operating-rate time series)
  - 中国石化/中石油 internal monthly production data (if customer has access)
- [ ] Does the customer already subscribe to any of these?
- [ ] Is there an internal database (ERP / MES) where downstream customer production volumes are recorded? (Even monthly production ≈ proxy for operating rate.)

**2. Access method**
- [ ] API / database dump / Excel attachment / scraped web page?
- [ ] If paid feed: what is the delivery format (SFTP CSV, API JSON, email Excel)?
- [ ] If scraping: is it permitted by the source's terms of service? (Do NOT scrape without confirming licensing.)
- [ ] Authentication required? (token, login, IP whitelist)

**3. Schema**
- [ ] What is the granularity? (weekly % per product? per producer? per region?)
- [ ] What columns/fields are available? (operating_rate_pct, capacity_t, output_t, region, producer, week_ending_date)
- [ ] How far back does history go? (need ≥ 2 years for seasonal patterns)
- [ ] How are missing weeks handled? (holiday weeks, plant shutdowns)

**4. Cadence**
- [ ] How often is it published? (weekly Thursday? monthly?)
- [ ] What is the lag? (e.g., data for week ending Friday published next Wednesday = 5-day lag)
- [ ] Is there a real-time alert when a major plant announces a turnaround?

**5. Cost & licensing**
- [ ] Annual subscription cost?
- [ ] Can the data be stored in our DB (or is it view-only / licensed per-user)?
- [ ] Can it be used to drive automated forecasts, or is redistribution restricted?

### Loader spec (once feed is READY)
- New `OperatingRateLoader` in `exogenous_loaders.py` following F1 pattern.
- Returns DataFrame: columns `{product}_op_rate` indexed by week-ending date.
- Derived metrics (in a new `operating_signal.py`, mirroring `demand_signal.py`): 4-week rolling op-rate, YoY change, rate-vs-price divergence.
- Evidence-pack field: `downstream_utilization` → powers a real 供需研判 sentence.

---

## T3.2 — Port & social inventory (库存)

**Why it matters:** Inventory level is the supply-side counterpart to operating rate. High inventory + falling operating rate = bearish; low inventory + high operating rate = tight/price-supportive. Human reports cite 华东/华南 port stock levels weekly.

### Questions to answer

**1. Source identification**
- [ ] Which source publishes port/social inventory for our products? Candidates:
  - 隆众资讯 — weekly port inventory reports (华东港存)
  - 卓创资讯 — social inventory (社会库存) surveys
  - Vortexa / Kpler — tank-level data (more expensive, real-time)
  - Customer's own WMS/ERP warehouse records (internal inventory only)
- [ ] Does the customer track their own inventory + competitors' (via market intel)?

**2. Access method**
- [ ] Same questions as T3.1 §2 (API/dump/Excel/scrape, format, auth).

**3. Schema**
- [ ] Granularity: weekly tons per port? per region? per product?
- [ ] Columns: inventory_t, port_name, region, product, week_ending_date, capacity_t?
- [ ] History depth (≥ 2 years)?
- [ ] Distinction between producer inventory / trader inventory / port inventory?

**4. Cadence**
- [ ] Publish frequency + lag (same questions as T3.1 §4).

**5. Cost & licensing**
- [ ] Same questions as T3.1 §5.

### Loader spec (once feed is READY)
- New `InventoryLoader` in `exogenous_loaders.py`.
- Returns DataFrame: `{product}_inventory_t` indexed by date.
- Derived metrics (new `inventory_signal.py`): 4-week inventory change %, inventory-vs-price divergence, days-of-supply (inventory / avg daily consumption).
- Evidence-pack field: `inventory_pressure` → 供需研判 supply-side sentence.

---

## T3.3 — Import & competitor offer prices (进口价/竞品报价)

**Why it matters:** Import parity is a price ceiling for domestic products. When domestic price > import parity (CIF + duty + freight), imports flow in and cap further rises. Human reports cite 东南亚/韩国/中东 import offers and competitor (中石化/中石油) list prices.

### Questions to answer

**1. Source identification**
- [ ] Which source publishes import offers? Candidates:
  - ICIS / Argus — CFR China price assessments
  - 隆众/卓创 — domestic + import price comparisons
  - China Customs (海关总署) monthly import volume + value (free, but monthly + 20-day lag)
  - Customer's procurement records of import offers received
- [ ] Are competitor (中石化/中石油) list prices published? (often on their sales portals / WeChat)

**2. Access method**
- [ ] Same questions as T3.1 §2.

**3. Schema**
- [ ] Granularity: daily/weekly CIF or CFR price per product per origin?
- [ ] Columns: price, origin_country, incoterm (CIF/CFR/FOB), duty_included, product, date?
- [ ] History depth?
- [ ] Customs data: monthly import volume (tons) + average import price (USD/t) per HS code — is the HS code mapping known for each product?

**4. Cadence**
- [ ] Daily (ICIS/Argus) vs weekly (隆众) vs monthly (Customs)?
- [ ] Lag per source?

**5. Cost & licensing**
- [ ] Same questions as T3.1 §5. (ICIS/Argus are expensive — confirm budget.)

### Loader spec (once feed is READY)
- New `ImportPriceLoader` in `exogenous_loaders.py`.
- Returns DataFrame: `import_price_cny` (converted via `FxLoader`) indexed by date.
- Derived metric: `import_parity_gap` = (domestic price − import parity) / domestic price → price ceiling signal.
- Evidence-pack field: `import_pressure` → risk section sentence ("进口窗口打开,价格上行空间受限").

---

## Action items to unblock (do today)

1. **Assign an owner** for each of the three domains (likely a procurement/market-intel person on the customer side, not engineering).
2. **Confirm existing subscriptions**: ask the customer *today* which of 隆众/卓创/ICIS/Argus/Wind they already pay for. A "yes, we get 隆众 weekly" answer unblocks T3.1 + T3.2 instantly.
3. **HS code mapping**: for T3.3 Customs data, identify the HS codes for product_d, product_f, PRODUCT_B, styrene, blowing agent, resin. This is free data; only the mapping is needed.
4. **Internal-data audit**: check whether the customer's ERP/MES already records downstream production volumes or inventory — these may be free proxies for T3.1/T3.2.
5. **Re-scope if needed**: if all three remain `[BLOCKED]` after 2 weeks, Wave 3 should be descoped and the roadmap value reassessed — Tier 1+2 still deliver the majority of forecast + brief improvement without external feeds.

---

## When a feed becomes READY

For each feed that moves to `[READY]`:
1. Create a spec under `docs/superpowers/specs/` for that loader (T3.1/T3.2/T3.3).
2. Follow the F1 plan's Task 1/2 pattern exactly (loader class + pure signal module + evidence-pack field + tests).
3. The loader + signal module + pack field is ~1 day of engineering per feed; the data discovery above is the actual blocker.
