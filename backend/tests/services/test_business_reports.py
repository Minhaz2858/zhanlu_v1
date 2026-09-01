"""Tests for business_reports — deterministic business narratives."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "db" / "business_reports.py"


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("business_reports", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CONTRACT_ROWS = [
    {
        "partner_name": " Qingdao Weichem Chemical Co.,Ltd",
        "FMATERIALID": "201000036",
        "FBILLNO": "YSK-YXHT3-202607-0402",
        "contract_id": 122016,
        "contract_quantity": 14,
        "contract_price": 8270.14,
        "shipment_quantity": 14,
        "total_revenue": 115781.97,
        "execution_rate_pct": 100,
        "shipment_date": "2026-07-22T00:00:00",
        "prior_month_contract_qty": 0,
        "prior_month_shipment_qty": 0,
        "current_month_contract_qty": 14,
        "current_month_shipment_qty": 14,
    },
    {
        "partner_name": "Kolon Industries Co. Ltd",
        "FMATERIALID": "202000003",
        "FBILLNO": "YSK-YXHT5-202606-0804",
        "contract_id": 121792,
        "contract_quantity": 506,
        "contract_price": 7498.37,
        "shipment_quantity": 495.42,
        "total_revenue": 3714842.47,
        "execution_rate_pct": 97.9,
        "shipment_date": "2026-06-16T00:00:00",
        "prior_month_contract_qty": 506,
        "prior_month_shipment_qty": 495.42,
        "current_month_contract_qty": 0,
        "current_month_shipment_qty": 0,
    },
    {
        "partner_name": "北京万邦达环保技术股份有限公司",
        "FMATERIALID": "121957",
        "FBILLNO": "GY-YXHT3-202607-0025",
        "contract_id": 121957,
        "contract_quantity": 34.2,
        "contract_price": 13000,
        "shipment_quantity": 60,
        "total_revenue": 780000,
        "execution_rate_pct": 175.4,
        "shipment_date": "2026-07-14T00:00:00",
        "prior_month_contract_qty": 0,
        "prior_month_shipment_qty": 0,
        "current_month_contract_qty": 34.2,
        "current_month_shipment_qty": 60,
    },
    {
        "partner_name": "TRIBUTE ENERGY INC.",
        "FMATERIALID": "121923",
        "FBILLNO": "YSK-YXHT5-202607-0881",
        "contract_id": 121923,
        "contract_quantity": 135,
        "contract_price": 9733.58,
        "shipment_quantity": 89.54,
        "total_revenue": 871544.84,
        "execution_rate_pct": 66.3,
        "shipment_date": "2026-07-09T00:00:00",
        "prior_month_contract_qty": 0,
        "prior_month_shipment_qty": 0,
        "current_month_contract_qty": 135,
        "current_month_shipment_qty": 89.54,
    },
]


class TestContractReport:
    def test_builds_report_for_contract_question(self, br):
        out = br.try_build_business_report(
            "give me Contract Performance for last month report in docx file",
            CONTRACT_ROWS,
            "aipdp_data_warehouse_prod",
        )
        assert out is not None
        assert "# Contract Performance" in out
        assert "## Executive Summary" in out
        assert "## Key Figures" in out
        assert "## Top Customers" in out
        assert "execution rate" in out.lower()
        # Aggregate rate: (14+495.42+60+89.54)/(14+506+34.2+135) = 658.96/689.2 = 95.6%
        assert "95.6%" in out
        assert "TRIBUTE ENERGY" in out  # risk row (66.3% < 80%)
        assert "## Month-over-Month" in out
        # MoM: prior 506 → current 14+34.2+135 = 183.2 → -63.8%
        assert "63.8%" in out

    def test_builds_report_for_contract_data_without_keywords(self, br):
        out = br.try_build_business_report(
            "give me a report please",
            CONTRACT_ROWS,
            "src",
        )
        assert out is not None
        assert "## Executive Summary" in out

    def test_returns_none_for_sales_data(self, br):
        sales_rows = [
            {"CUST_NAME": "ACME", "FALLAMOUNT": 100.0, "FQTY_ORIGIN": 5.0, "PLANDATE": "2026-07-01"}
        ]
        out = br.try_build_business_report(
            "give me top customer for last month in docx file",
            sales_rows,
            "sales",
        )
        assert out is None

    def test_empty_rows_returns_none(self, br):
        assert br.try_build_business_report("contract performance", [], "src") is None

    def test_raw_erp_column_names_work(self, br):
        raw_rows = [
            {
                "CUST_NAME": "ACME",
                "contract_fqty": 100,
                "out_fqty": 80,
                "out_amount": 8000,
                "contract_amount": 10000,
                "out_notoutqty": 20,
                "FDATE": "2026-07-01",
            }
        ]
        out = br.try_build_business_report("合同履约情况", raw_rows, "erp")
        assert out is not None
        assert "80.0%" in out
        assert "¥10,000.00" in out
        assert "20.00" in out  # outstanding

    # 2026-08-26 (run3 regression): the LLM data agent aliases aggregated
    # columns with business-y names (SUM(contract_fqty) AS total_sales_volume,
    # SUM(contract_amount) AS revenue, SUM/SUM AS average_execution_price).
    # The deterministic report must still resolve qty/amount/price and
    # produce REAL numbers (not 0.00/—) so the docx table matches the KPIs.
    def test_llm_aggregate_aliases_resolve(self, br):
        rows = [
            {
                "product_type": "液体",
                "CUST_NAME": "中国石化化工销售有限公司华中分公司",
                "total_sales_volume": 2800.0,
                "revenue": 31752000.0,
                "average_execution_price": 11340.0,
                "order_count": 1,
            },
            {
                "product_type": "固体",
                "CUST_NAME": "山东齐昤新材料有限公司",
                "total_sales_volume": 115.0,
                "revenue": 1081000.0,
                "average_execution_price": 9400.0,
                "order_count": 1,
            },
            {
                "product_type": "液体",
                "CUST_NAME": "广州市华泽新材料有限公司",
                "total_sales_volume": 1480.0,
                "revenue": 8468000.0,
                "average_execution_price": 5721.62,
                "order_count": 3,
            },
        ]
        out = br.try_build_business_report(
            "Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # total qty = 2800 + 115 + 1480 = 4395
        assert "4,395.00" in out
        # total revenue = 31,752,000 + 1,081,000 + 8,468,000 = 41,301,000
        assert "¥41,301,000.00" in out
        # top customer by value is 中国石化…
        assert "中国石化" in out
        # every customer row must carry a REAL value — no standalone
        # zero cells (a bare "| 0.00 |" / "| 0.0% |" would mean the
        # alias resolution failed again). Numbers like "¥41,301,000.00"
        # legitimately contain "0.00" so check the cell pattern, not substring.
        assert not re.search(r"\| 0\.00 \|", out), out
        assert not re.search(r"\| 0\.0% \|", out), out
        # revenue must NOT be multiplied by qty (amount-vs-unit-price rule)
        assert "¥90,000,000" not in out  # 11340*2800 double-count guard

    def test_llm_revenue_alias_treated_as_total_not_unit_price(self, br):
        """SUM(contract_amount) AS revenue is a TOTAL — the builder must sum
        it directly, never multiply by qty (observed ¥2.098B from a ¥5.2M
        portfolio when a total-named column was treated as per-unit price)."""
        rows = [
            {"CUST_NAME": "A", "total_sales_volume": 500.0, "revenue": 500000.0},
            {"CUST_NAME": "B", "total_sales_volume": 300.0, "revenue": 300000.0},
        ]
        out = br.try_build_business_report("contract performance", rows, "erp")
        assert out is not None
        assert "¥800,000.00" in out
        assert "¥400,000,000" not in out  # 800*500k would be the wrong path

    def test_contract_revenue_and_shipment_revenue_aliases(self, br):
        """Run4 real shape: the LLM aliased SUM(contract_amount) AS
        contract_revenue and SUM(out_amount) AS shipment_revenue. Both must
        resolve: contract_revenue → contract value, shipment_revenue →
        delivered value. A NULL shipment_quantity is truthful zero delivery
        (not a bug), but contract values must never be missing."""
        rows = [
            {"CUST_NAME": "A", "contract_quantity": 3000.0, "shipment_quantity": 319.6,
             "contract_revenue": 38400000.0, "shipment_revenue": 6342400.0},
            {"CUST_NAME": "B", "contract_quantity": 5000.0, "shipment_quantity": None,
             "contract_revenue": 38150000.0, "shipment_revenue": 0.0},
            {"CUST_NAME": "C", "contract_quantity": 5000.0, "shipment_quantity": 3420.92,
             "contract_revenue": 32500000.0, "shipment_revenue": 25812670.0},
        ]
        out = br.try_build_business_report(
            "Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # 38.4M + 38.15M + 32.5M = 109,050,000
        assert "¥109,050,000.00" in out
        # 6,342,400 + 0 + 25,812,670 = 32,155,070
        assert "¥32,155,070.00" in out
        # per-customer value present (B = 38,150,000)
        assert "¥38,150,000.00" in out
        # no missing contract values
        assert "| — |" not in out
        # never multiply revenue by qty: 38.4M × 3000 = 115.2B
        assert "¥115,200,000,000" not in out

    def test_delivered_value_not_duplicated_from_contract_column(self, br):
        """Run6: the LLM aliased SUM(contract_amount) AS total_revenue and
        the query has NO separate delivered-amount column. The builder must
        NOT render 'Delivered value' from the same column that fed
        'Contracted value' — that duplicates the number. Delivered value is
        only meaningful when it resolves to a DIFFERENT column."""
        rows = [
            {"customer": "A", "total_volume": 3000.0, "total_revenue": 38400000.0,
             "average_price": 12800.0, "order_count": 1},
            {"customer": "B", "total_volume": 5000.0, "total_revenue": 38150000.0,
             "average_price": 7630.0, "order_count": 1},
        ]
        out = br.try_build_business_report(
            "Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # 38.4M + 38.15M = 76,550,000
        assert "¥76,550,000.00" in out
        # no delivered-value duplication from the same column
        assert "Delivered value" not in out
        # no missing contract values
        assert "| — |" not in out

    def test_delivery_quantities_and_undelivered_aliases(self, br):
        """Run7: the LLM aliased SUM(out_fqty) AS delivery_quantities and
        SUM(out_notoutqty) AS undelivered_quantities. Both must resolve:
        delivery_quantities → delivered qty + execution rate,
        undelivered_quantities → outstanding to deliver."""
        rows = [
            {"product_type": "液体", "CUST_NAME": "A", "order_counts": 1,
             "total_sales_volume": 2800.0, "revenue": 31752000.0,
             "delivery_quantities": 2650.0, "undelivered_quantities": 150.0,
             "average_execution_price": 11340.0},
            {"product_type": "固体", "CUST_NAME": "B", "order_counts": 1,
             "total_sales_volume": 115.0, "revenue": 1081000.0,
             "delivery_quantities": 100.0, "undelivered_quantities": 15.0,
             "average_execution_price": 9400.0},
        ]
        out = br.try_build_business_report(
            "Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # delivered qty 2650 + 100 = 2750; exec rate 2750/2915 = 94.3%
        assert "2,750.00" in out
        assert "94.3%" in out
        # contract value 31,752,000 + 1,081,000 = 32,833,000
        assert "¥32,833,000.00" in out
        # outstanding 150 + 15 = 165
        assert "165.00" in out

    def test_contract_price_misnamed_total_promotion(self, br):
        """User docx shape (2026-08-27, artifact from 02:10): the LLM aliased
        contract_amount AS contract_price (a TOTAL misnamed as price) and
        out_amount AS revenue (delivered). The resolver picks revenue as
        contracted value → "Contracted value" shows DELIVERED value. The
        data-driven discriminator must promote contract_price (price ≈
        amount per row ⇒ it holds TOTALS) to contracted value and demote
        revenue to delivered value. NEVER price×qty (¥458B double-count)."""
        rows = [
            {"contract_id": 121917, "customer_name": "A", "product_name": "P1",
             "contract_quantity": 400.0, "contract_price": 2540000.0,
             "shipment_quantity": 386.44, "revenue": 2453894.0},
            {"contract_id": 121952, "customer_name": "B", "product_name": "P1",
             "contract_quantity": 160.0, "contract_price": 1024000.0,
             "shipment_quantity": 159.33, "revenue": 1019712.0},
            {"contract_id": 121888, "customer_name": "C", "product_name": "P2",
             "contract_quantity": 32.0, "contract_price": 300800.0,
             "shipment_quantity": 32.0, "revenue": 300800.0},
            {"contract_id": 121963, "customer_name": "D", "product_name": "P1",
             "contract_quantity": 65.0, "contract_price": 416000.0,
             "shipment_quantity": 64.82, "revenue": 414848.0},
        ]
        out = br.try_build_business_report(
            "give me Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # contracted = SUM(contract_price) = 2,540,000+1,024,000+300,800+416,000
        assert "¥4,280,800.00" in out
        # delivered = SUM(revenue) = 2,453,894+1,019,712+300,800+414,848
        assert "¥4,189,254.00" in out
        # NEVER price×qty: 2,540,000 × 400 = 1,016,000,000
        assert "¥1,016,000,000" not in out
        # exec rate = (386.44+159.33+32+64.82) / (400+160+32+65) = 642.59/657 = 97.8%
        assert "97.8%" in out

    def test_total_shipped_qty_alias_and_aggregate_rate(self, br):
        """Run9: the LLM aliased SUM(out_fqty) AS total_shipped_qty and
        included a per-row fulfillment_rate_pct. The shipped_qty alias must
        resolve as delivered qty so the EXECUTION RATE is the aggregate
        (SUM(shipped)/SUM(contracted)), never the average of per-row rates
        (which skews small contracts — 68.8% vs the true 47.0%)."""
        rows = [
            {"product_type": "液体", "CUST_NAME": "A", "total_contract_qty": 3000.0,
             "total_shipped_qty": 319.6, "total_contract_price": 38400000.0,
             "total_revenue": 6342400.0, "fulfillment_rate_pct": 10.7,
             "under_delivery_qty": 2680.4},
            {"product_type": "固体", "CUST_NAME": "B", "total_contract_qty": 5000.0,
             "total_shipped_qty": 3420.92, "total_contract_price": 32500000.0,
             "total_revenue": 25812670.0, "fulfillment_rate_pct": 68.4,
             "under_delivery_qty": 1579.08},
        ]
        out = br.try_build_business_report(
            "Contract performance for last month report",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is not None
        # aggregate rate = (319.6+3420.92)/(3000+5000) = 3740.52/8000 = 46.8%
        assert "46.8%" in out
        # NOT the average of 10.7 and 68.4 (= 39.55%)
        assert "39.6%" not in out
        # contracted value = 38.4M + 32.5M = 70.9M
        assert "¥70,900,000.00" in out
        # delivered qty = 319.6 + 3420.92
        assert "3,740.52" in out

    def test_delivered_only_query_refused(self, br):
        """Run10/11: the LLM answered a contract-performance request with a
        DELIVERED-ONLY query (order_number/customer/product/delivered_quantity/
        unit_price/revenue — no contract_fqty/contract_amount). The builder
        must REFUSE (return None) so the caller's generic fallback describes
        the delivered data honestly — never fabricate 'Contracted Qty 0.00'
        or mislabel delivered revenue as 'Contracted value'."""
        rows = [
            {"order_number": "YSK-1", "customer_name": "A", "product_name": "P1",
             "delivery_plan_date": "2026-08-05", "delivered_quantity": 3109.39,
             "unit_price": 12214.0, "revenue": 37978618.6},
            {"order_number": "YSK-2", "customer_name": "B", "product_name": "P2",
             "delivery_plan_date": "2026-08-10", "delivered_quantity": 3420.92,
             "unit_price": 7543.0, "revenue": 25812670.0},
        ]
        out = br.try_build_business_report(
            "give me Contract Performance for last month report in docx file",
            rows, "aipdp_data_warehouse_prod",
        )
        assert out is None
