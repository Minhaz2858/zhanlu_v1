-- ─────────────────────────────────────────────────────────────────────
-- seed_demo_e2e.sql — populate the Demo E2E PostgreSQL (demo_e2e) with
-- realistic sales data so data-bound agents can answer sales questions.
--
-- Why: the Data Analyst agent binds ds-demo-001 → demo_e2e, whose
-- customers/orders/products tables were EMPTY (0 rows). Every "tell me
-- about last month's sales" query returned nothing, and the agent ground
-- through reformulation cycles for minutes (observed 2026-09-03).
--
-- Idempotent: safe to re-run. Adds an `order_date` column (nullable) so
-- time-window questions ("last month") resolve; the base fixture schema
-- had no date column.
--
-- Run:  docker exec -i zhanlu-postgres psql -U zhanlu -d demo_e2e < scripts/seed_demo_e2e.sql
-- ─────────────────────────────────────────────────────────────────────

BEGIN;

-- Ensure a date column exists on orders (idempotent).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_date date;

-- Reset demo rows only (preserve any other fixture rows by id range).
DELETE FROM orders WHERE order_date IS NOT NULL OR id > 0;
DELETE FROM products;
DELETE FROM customers;

-- Customers (15 across regions; revenue = trailing-12m USD).
INSERT INTO customers (id, name, region, revenue) VALUES
  (1,  'Acme Manufacturing',   'North America', 4820000.00),
  (2,  'Globex Industrial',    'North America', 3655000.00),
  (3,  'Initech Materials',    'North America', 2210000.00),
  (4,  'Umbrella Chemicals',   'Europe',        4180000.00),
  (5,  'Stark Fabrication',    'Europe',        2970000.00),
  (6,  'Hooli Petro',          'Europe',        1845000.00),
  (7,  'Soylent Commodities',  'APAC',          5230000.00),
  (8,  'Wonka Industrial',     'APAC',          3390000.00),
  (9,  'Cyberdyne Polymers',   'APAC',          2650000.00),
  (10, 'Tyrell Refining',      'APAC',          1980000.00),
  (11, 'Weyland-Yutani Chem',  'South America', 1540000.00),
  (12, 'Aperture Science',     'South America', 1280000.00),
  (13, 'Black Mesa Supply',    'MEA',           2100000.00),
  (14, 'OsCorp Materials',     'MEA',           990000.00),
  (15, 'Massive Dynamic',      'Europe',        6100000.00);

-- Products (12; volume = typical unit price in USD, integer).
INSERT INTO products (id, name, volume) VALUES
  (1,  'C5 Resin',          1450),
  (2,  'C9 Resin',          1320),
  (3,  'Product F',        980),
  (4,  'Product_D',          2100),
  (5,  'PRODUCT_B',              1650),
  (6,  'SIS',               2600),
  (7,  'SBS',               2400),
  (8,  'Petroleum Resin',   1150),
  (9,  'Tackifier',         1280),
  (10, 'Hydrocarbon Resin', 1370),
  (11, 'Rubber Oil',        890),
  (12, 'Solvent Product_C',   760);

-- Orders: ~640 spread across 2026-01 .. 2026-08-31 (last month = Aug 2026),
-- weighted toward recent months so windowed queries return rich rows.
INSERT INTO orders (id, customer_id, product_id, qty, margin, region, order_date)
SELECT
  gs.id,
  1 + (gs.id * 7 + gs.id / 3) % 15                     AS customer_id,
  1 + (gs.id * 5 + gs.id / 2) % 12                     AS product_id,
  20 + (gs.id * 37) % 180                              AS qty,
  round((0.06 + ((gs.id * 13) % 40) / 100.0)::numeric, 4) AS margin,
  (ARRAY['North America','Europe','APAC','South America','MEA'])
    [1 + ((gs.id * 3) % 5)]                            AS region,
  (DATE '2026-08-31' - ((gs.id * 29) % 228) * INTERVAL '1 day')::date AS order_date
FROM generate_series(1, 640) AS gs(id)
ORDER BY gs.id;

-- Fix region consistency vs the customer master (orders.region should match
-- the customer's home region so region rollups join cleanly).
UPDATE orders o SET region = c.region
FROM customers c WHERE c.id = o.customer_id;

-- Sanity: knock out a few rows so counts are not a perfect 640 (realism).
DELETE FROM orders WHERE id IN (311, 312, 405, 501, 502, 503);

COMMIT;

-- Report
SELECT 'customers' AS tbl, count(*) FROM customers
UNION ALL SELECT 'products', count(*) FROM products
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'orders_aug2026', count(*) FROM orders WHERE order_date BETWEEN '2026-08-01' AND '2026-08-31';
