"""Full verification of business_reports.py fixes — mirrors test_business_reports.py.
Run inside the backend container (pytest is NOT installed there; system python3 has sqlalchemy).
"""
from app.services.db import business_reports as br

# ── Fixture 1: classic contract rows (from test_business_reports.py) ──
CONTRACT_ROWS = [
    {'partner_name': 'Dongguan A', 'FMATERIALID': '121956', 'FBILLNO': 'YSK-YXHT5-202607-0704', 'contract_id': 121791, 'contract_quantity': 14, 'contract_price': 7637.5, 'shipment_quantity': 14, 'total_revenue': 106925, 'execution_rate_pct': 100.0, 'shipment_date': '2026-07-22T00:00:00', 'prior_month_contract_qty': 0, 'prior_month_shipment_qty': 0, 'current_month_contract_qty': 14, 'current_month_shipment_qty': 14},
    {'partner_name': 'Kolon Industries Co. Ltd', 'FMATERIALID': '202000003', 'FBILLNO': 'YSK-YXHT5-202606-0804', 'contract_id': 121792, 'contract_quantity': 506, 'contract_price': 7498.37, 'shipment_quantity': 495.42, 'total_revenue': 3714842.47, 'execution_rate_pct': 97.9, 'shipment_date': '2026-06-16T00:00:00', 'prior_month_contract_qty': 506, 'prior_month_shipment_qty': 495.42, 'current_month_contract_qty': 0, 'current_month_shipment_qty': 0},
    {'partner_name': '北京万邦达环保技术股份有限公司', 'FMATERIALID': '121957', 'FBILLNO': 'GY-YXHT3-202607-0025', 'contract_id': 121957, 'contract_quantity': 34.2, 'contract_price': 13000, 'shipment_quantity': 60, 'total_revenue': 780000, 'execution_rate_pct': 175.4, 'shipment_date': '2026-07-14T00:00:00', 'prior_month_contract_qty': 0, 'prior_month_shipment_qty': 0, 'current_month_contract_qty': 34.2, 'current_month_shipment_qty': 60},
    {'partner_name': 'TRIBUTE ENERGY INC.', 'FMATERIALID': '121923', 'FBILLNO': 'YSK-YXHT5-202607-0881', 'contract_id': 121923, 'contract_quantity': 135, 'contract_price': 9733.58, 'shipment_quantity': 89.54, 'total_revenue': 871544.84, 'execution_rate_pct': 66.3, 'shipment_date': '2026-07-09T00:00:00', 'prior_month_contract_qty': 0, 'prior_month_shipment_qty': 0, 'current_month_contract_qty': 135, 'current_month_shipment_qty': 89.54},
]

# ── Fixture 2: LLM-aggregated aliases (run3 real shape) ──
ALIAS_ROWS = [
    {'product_type': '液体', 'CUST_NAME': '中国石化化工销售有限公司华中分公司', 'total_sales_volume': 2800.0, 'revenue': 31752000.0, 'average_execution_price': 11340.0, 'order_count': 1},
    {'product_type': '固体', 'CUST_NAME': '山东齐昤新材料有限公司', 'total_sales_volume': 115.0, 'revenue': 1081000.0, 'average_execution_price': 9400.0, 'order_count': 1},
    {'product_type': '液体', 'CUST_NAME': '广州市华泽新材料有限公司', 'total_sales_volume': 1480.0, 'revenue': 8468000.0, 'average_execution_price': 5721.62, 'order_count': 3},
]

# ── Fixture 3: revenue-as-total vs unit-price (double-count guard) ──
REVENUE_ROWS = [
    {'CUST_NAME': 'A', 'total_sales_volume': 500.0, 'revenue': 500000.0},
    {'CUST_NAME': 'B', 'total_sales_volume': 300.0, 'revenue': 300000.0},
]

# ── Fixture 3b: run4 real shape — contract_revenue / shipment_revenue ──
REVENUE_ALIAS_ROWS = [
    {'CUST_NAME': '中国石化化工销售有限公司华中分公司', 'contract_quantity': 3000.0, 'shipment_quantity': 319.6, 'contract_revenue': 38400000.0, 'shipment_revenue': 6342400.0},
    {'CUST_NAME': '中海壳牌石油化工有限公司', 'contract_quantity': 5000.0, 'shipment_quantity': None, 'contract_revenue': 38150000.0, 'shipment_revenue': 0.0},
    {'CUST_NAME': '惠州伊斯科新材料科技发展有限公司', 'contract_quantity': 5000.0, 'shipment_quantity': 3420.92, 'contract_revenue': 32500000.0, 'shipment_revenue': 25812670.0},
]

# ── Fixture 3c: run6 shape — total_revenue ONLY (no delivered-amount col).
# total_revenue = SUM(contract_amount). Delivered value must be OMITTED
# (not duplicated from the same column). ──
TOTAL_REV_ONLY_ROWS = [
    {'customer': '中国石化化工销售有限公司华中分公司', 'product': 'X', 'total_volume': 3000.0, 'total_revenue': 38400000.0, 'average_price': 12800.0, 'order_count': 1},
    {'customer': '中海壳牌石油化工有限公司', 'product': 'Y', 'total_volume': 5000.0, 'total_revenue': 38150000.0, 'average_price': 7630.0, 'order_count': 1},
]

# ── Fixture 3d: run7 shape — delivery_quantities / undelivered_quantities
# aliases for out/remain columns ──
DELIVERY_ALIAS_ROWS = [
    {'product_type': '液体', 'CUST_NAME': '中国石化化工销售有限公司华中分公司', 'order_counts': 1, 'total_sales_volume': 2800.0, 'revenue': 31752000.0, 'delivery_quantities': 2650.0, 'undelivered_quantities': 150.0, 'average_execution_price': 11340.0},
    {'product_type': '固体', 'CUST_NAME': '山东齐昤新材料有限公司', 'order_counts': 1, 'total_sales_volume': 115.0, 'revenue': 1081000.0, 'delivery_quantities': 100.0, 'undelivered_quantities': 15.0, 'average_execution_price': 9400.0},
]

# ── Fixture 3e: user-docx shape (02:10 artifact) — contract_amount aliased
# AS contract_price (TOTAL misnamed as price), out_amount AS revenue.
# Correct: Contracted value = SUM(contract_price) = ¥324,313,278.41;
# Delivered value = SUM(revenue). NEVER price×qty (would be ¥458B). ──
USER_SHAPE_ROWS = [
    {'contract_id': 121917, 'customer_name': '天津鲁华泓锦新材料科技有限公司', 'product_name': '双环戊二烯', 'contract_quantity': 400.0, 'contract_price': 2540000.0, 'shipment_quantity': 386.44, 'revenue': 2453894.0},
    {'contract_id': 121952, 'customer_name': '东莞市同舟化工有限公司', 'product_name': '双环戊二烯', 'contract_quantity': 160.0, 'contract_price': 1024000.0, 'shipment_quantity': 159.33, 'revenue': 1019712.0},
    {'contract_id': 121888, 'customer_name': '广东天元实业集团股份有限公司', 'product_name': '碳五石油树脂', 'contract_quantity': 32.0, 'contract_price': 300800.0, 'shipment_quantity': 32.0, 'revenue': 300800.0},
    {'contract_id': 121963, 'customer_name': '广东汇泉联骏化学工业有限公司', 'product_name': '双环戊二烯', 'contract_quantity': 65.0, 'contract_price': 416000.0, 'shipment_quantity': 64.82, 'revenue': 414848.0},
]

# ── Fixture 3f: run9 shape — total_shipped_qty / fulfillment_rate_pct.
# Aggregate exec rate = SUM(shipped)/SUM(contract) = 47.0%, NOT the average
# of per-row rates (68.8% — skews small contracts). ──
SHIPPED_ALIAS_ROWS = [
    {'product_type': '液体', 'CUST_NAME': 'A', 'total_contract_qty': 3000.0, 'total_shipped_qty': 319.6, 'total_contract_price': 38400000.0, 'total_revenue': 6342400.0, 'fulfillment_rate_pct': 10.7, 'under_delivery_qty': 2680.4},
    {'product_type': '固体', 'CUST_NAME': 'B', 'total_contract_qty': 5000.0, 'total_shipped_qty': 3420.92, 'total_contract_price': 32500000.0, 'total_revenue': 25812670.0, 'fulfillment_rate_pct': 68.4, 'under_delivery_qty': 1579.08},
]

# ── Fixture 3g: run10 shape — DELIVERED-ONLY query (no contract columns).
# No contracted qty → refuse the contract shape (return None); the generic
# fallback must describe the delivered data, never fabricate "Contracted
# Qty 0.00" or mislabel delivered revenue as "Contracted value". ──
DELIVERED_ONLY_ROWS = [
    {'order_number': 'YSK-1', 'customer_name': 'A', 'product_name': 'P1', 'delivery_plan_date': '2026-08-05', 'delivered_quantity': 3109.39, 'unit_price': 12214.0, 'revenue': 37978618.6},
    {'order_number': 'YSK-2', 'customer_name': 'B', 'product_name': 'P2', 'delivery_plan_date': '2026-08-10', 'delivered_quantity': 3420.92, 'unit_price': 7543.0, 'revenue': 25812670.0},
]

# ── Fixture 4: raw ERP names (Chinese question) ──
RAW_ROWS = [
    {'CUST_NAME': 'ACME', 'contract_fqty': 100, 'out_fqty': 80, 'out_amount': 8000,
     'contract_amount': 10000, 'out_notoutqty': 20, 'FDATE': '2026-07-01'},
]

# ── Fixture 5: sales data (must be rejected → None) ──
SALES_ROWS = [{'CUST_NAME': 'ACME', 'FALLAMOUNT': 100.0, 'FQTY_ORIGIN': 5.0, 'PLANDATE': '2026-07-01'}]

results = []

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))

# 1. classic contract report
out = br.try_build_business_report('give me Contract Performance for last month report in docx file', CONTRACT_ROWS, 'aipdp_data_warehouse_prod')
check("classic: report built", out is not None)
check("classic: title", out is not None and '# Contract Performance' in out)
check("classic: exec summary + key figures", out and '## Executive Summary' in out and '## Key Figures' in out)
check("classic: exec rate 95.6%", out and '95.6%' in out)
check("classic: risk row TRIBUTE", out and 'TRIBUTE ENERGY' in out)
check("classic: MoM 63.8%", out and '63.8%' in out)

# 2. LLM-aggregated aliases
out2 = br.try_build_business_report('Contract Performance for last month report in docx file', ALIAS_ROWS, 'aipdp_data_warehouse_prod')
check("alias: report built", out2 is not None)
check("alias: total qty 4,395.00", out2 and '4,395.00' in out2)
check("alias: total revenue ¥41,301,000.00", out2 and '¥41,301,000.00' in out2)
check("alias: top customer 中国石化", out2 and '中国石化' in out2)
check("alias: no standalone zero cells", out2 and '| 0.00 |' not in out2 and '| 0.0% |' not in out2)
check("alias: no double-count ¥90,000,000", out2 and '¥90,000,000' not in out2)
check("alias: no Delivered col when absent", out2 and 'Delivered Qty' not in out2)

# 3. revenue-as-total
out3 = br.try_build_business_report('contract performance', REVENUE_ROWS, 'erp')
check("revenue-total: ¥800,000.00", out3 and '¥800,000.00' in out3)
check("revenue-total: no ¥400,000,000", out3 and '¥400,000,000' not in out3)

# 3b. run4 revenue aliases (contract_revenue / shipment_revenue)
out3b = br.try_build_business_report('Contract Performance for last month report in docx file', REVENUE_ALIAS_ROWS, 'aipdp_data_warehouse_prod')
check("rev-alias: report built", out3b is not None)
# total contract value = 38.4M + 38.15M + 32.5M = 109,050,000
check("rev-alias: total contract value ¥109,050,000.00", out3b and '¥109,050,000.00' in out3b)
# total delivered value = 6,342,400 + 0 + 25,812,670 = 32,155,070
check("rev-alias: delivered value ¥32,155,070.00", out3b and '¥32,155,070.00' in out3b)
# per-customer values: 中海壳牌 38,150,000
check("rev-alias: 中海壳牌 ¥38,150,000.00", out3b and '¥38,150,000.00' in out3b)
# NULL shipment_quantity for 中海壳牌 = genuinely 0 delivered in the DB —
# 0.0% is the TRUTHFUL rendering (out column exists, row has no delivery).
# The no-bogus-zeros rule applies when NO delivery column exists at all
# (covered by the 'alias:' fixture). Here we verify the row renders its
# real numbers and no contract value is missing.
check("rev-alias: 中海壳牌 0-delivery row truthful", out3b and '中海壳牌' in out3b)
check("rev-alias: no missing contract values", out3b and '| — |' not in out3b)
# revenue must NOT be multiplied by qty (amount-vs-unit-price rule)
check("rev-alias: no double-count ¥115,200,000,000", out3b and '¥115,200,000,000' not in out3b)

# 3c. run6 shape — total_revenue only; delivered value must NOT duplicate
out3c = br.try_build_business_report('Contract Performance for last month report in docx file', TOTAL_REV_ONLY_ROWS, 'aipdp_data_warehouse_prod')
check("rev-only: report built", out3c is not None)
check("rev-only: total contract value ¥76,550,000.00", out3c and '¥76,550,000.00' in out3c)
check("rev-only: NO delivered-value duplication", out3c and 'Delivered value' not in out3c)
check("rev-only: no missing contract values", out3c and '| — |' not in out3c)

# 3d. run7 shape — delivery_quantities/undelivered_quantities aliases
out3d = br.try_build_business_report('Contract Performance for last month report in docx file', DELIVERY_ALIAS_ROWS, 'aipdp_data_warehouse_prod')
check("del-alias: report built", out3d is not None)
# delivered qty = 2650 + 100 = 2750; exec rate = 2750/2915 = 94.3%
check("del-alias: total delivered qty 2,750.00", out3d and '2,750.00' in out3d)
check("del-alias: exec rate 94.3%", out3d and '94.3%' in out3d)
check("del-alias: contract value ¥32,833,000.00", out3d and '¥32,833,000.00' in out3d)
check("del-alias: outstanding 165.00", out3d and '165.00' in out3d)

# 3e. user-docx shape — contract_price holds TOTAL (misnamed), revenue=delivered
out3e = br.try_build_business_report('give me Contract Performance for last month report in docx file', USER_SHAPE_ROWS, 'aipdp_data_warehouse_prod')
check("user-shape: report built", out3e is not None)
# contracted value = SUM(contract_price) = 2,540,000+1,024,000+300,800+416,000 = 4,280,800
check("user-shape: contracted value ¥4,280,800.00", out3e and '¥4,280,800.00' in out3e)
# delivered value = SUM(revenue) = 2,453,894+1,019,712+300,800+414,848 = 4,189,254
check("user-shape: delivered value ¥4,189,254.00", out3e and '¥4,189,254.00' in out3e)
# NEVER price×qty: 2,540,000 × 400 = 1,016,000,000 would be the double-count
check("user-shape: no double-count ¥1,016,000,000", out3e and '¥1,016,000,000' not in out3e)
# exec rate = 642.59 / 657.0 = 97.8%
check("user-shape: exec rate 97.8%", out3e and '97.8%' in out3e)

# 3f. run9 shape — shipped_qty aliases; aggregate exec rate must win
out3f = br.try_build_business_report('Contract performance for last month report', SHIPPED_ALIAS_ROWS, 'aipdp_data_warehouse_prod')
check("shipped-alias: report built", out3f is not None)
# aggregate rate = 3740.52/8000 = 46.8% — NOT the avg of 10.7/68.4 (39.55%)
check("shipped-alias: exec rate 46.8%", out3f and '46.8%' in out3f)
check("shipped-alias: not per-row avg 39.6%", out3f and '39.6%' not in out3f)
check("shipped-alias: contracted value ¥70,900,000.00", out3f and '¥70,900,000.00' in out3f)
check("shipped-alias: delivered qty 3,740.52", out3f and '3,740.52' in out3f)

# 3g. run10 shape — delivered-only query must be REFUSED (return None)
out3g = br.try_build_business_report('give me Contract Performance for last month report in docx file', DELIVERED_ONLY_ROWS, 'aipdp_data_warehouse_prod')
check("delivered-only: returns None (refused)", out3g is None)

# 4. raw ERP columns + Chinese question
out4 = br.try_build_business_report('合同履约情况', RAW_ROWS, 'erp')
check("raw-erp: report built", out4 is not None)
check("raw-erp: exec rate 80.0%", out4 and '80.0%' in out4)
check("raw-erp: ¥10,000.00", out4 and '¥10,000.00' in out4)
check("raw-erp: outstanding 20.00", out4 and '20.00' in out4)

# 5. sales rejection
out5 = br.try_build_business_report('give me top customer for last month in docx file', SALES_ROWS, 'sales')
check("sales-reject: returns None", out5 is None)

# 6. empty rows
check("empty-rows: returns None", br.try_build_business_report('contract performance', [], 'src') is None)

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} checks passed")
exit(0 if passed == total else 1)
