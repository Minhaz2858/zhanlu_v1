"""Tests for report_service.py using synthetic row data.

Covers: row loading/normalization, week helpers, YOY computation,
customer exclusion, dedup, execution rate, unit conversions.
"""
import pytest
from datetime import date, datetime, timedelta

from app.services.report_service import (
    _safe_float,
    _week_label,
    _week_monday,
    _row_sales_amount,
    _resolve_report_org,
    EXCLUDED_GROUP_ORGS,
    build_customer_sales_report,
    build_contract_performance_report,
)


# ── Row factory ──────────────────────────────────────────────────────────

def _make_row(
    *,
    fdate: str = "2025-06-15",
    material_name: str = "C5石油树脂",
    cust_name: str = "青岛橡塑科技",
    out_fqty: float = 50.0,
    contract_amount: float = 600000,
    order_tax_unit_price: float = 12000,
    out_amount: float = 600000,
    out_notoutqty: float = 0,
    contract_fqty: float = 100.0,
    fbillno: str = "BILL-001",
    product_type: str = "resin",
    grade: str = "A",
    fdeliverydate: str = "2025-07-01",
    org_name: str = "惠州伊斯科",
    ftaxprice_hit_count: int = 1,
    fprice_hit_count: int = 0,
    **kwargs,
) -> dict:
    return {
        "ORG_NAME": org_name,
        "FID": kwargs.get("FID", ""),
        "FDATE": fdate,
        "FVALISTARTDATE": kwargs.get("FVALISTARTDATE", ""),
        "FVALIENDDATE": kwargs.get("FVALIENDDATE", ""),
        "FDELIVERYDATE": fdeliverydate,
        "contract_entry_id": kwargs.get("contract_entry_id", ""),
        "FBILLNO": fbillno,
        "FNAME": kwargs.get("FNAME", "销售记录"),
        "CUST_NAME": cust_name,
        "FHEADDELIVERYWAY": kwargs.get("FHEADDELIVERYWAY", ""),
        "product_type": product_type,
        "material_code": kwargs.get("material_code", "C5-001"),
        "material_name": material_name,
        "material_unit": kwargs.get("material_unit", "吨"),
        "currency": kwargs.get("currency", "CNY"),
        "grade": grade,
        "contract_fqty": contract_fqty,
        "contract_amount": contract_amount,
        "contract_notdofqty": out_notoutqty,
        "ORDERQTY": contract_fqty,
        "ORDERAMOUNT": contract_amount,
        "ftaxprice_hit_count": ftaxprice_hit_count,
        "fprice_hit_count": fprice_hit_count,
        "order_tax_unit_price": order_tax_unit_price,
        "plan_fqty": kwargs.get("plan_fqty", 0),
        "out_fqty": out_fqty,
        "out_amount": out_amount,
        "out_notoutqty": out_notoutqty,
    }


# ── Helpers ──────────────────────────────────────────────────────────────

def test_safe_float():
    assert _safe_float("123.45") == 123.45
    assert _safe_float(None) == 0.0
    assert _safe_float("") == 0.0
    assert _safe_float("abc") == 0.0


def test_week_label():
    # 2025-06-15 is a Sunday → week starts Monday 06-09
    assert _week_label("2025-06-15") == "6.9-6.15"

    # 2025-06-09 is a Monday → week starts 06-09
    assert _week_label("2025-06-09") == "6.9-6.15"


def test_week_monday():
    assert _week_monday("2025-06-15") == "2025-06-09"
    assert _week_monday("2025-06-09") == "2025-06-09"


def test_row_sales_amount_priority():
    # qty > 0 and unit_price > 0 → qty * unit
    r = _make_row(out_fqty=10, order_tax_unit_price=8000, out_amount=70000, contract_amount=90000)
    assert _row_sales_amount(r) == 80000

    # unit_price == 0 → out_amount
    r2 = _make_row(out_fqty=10, order_tax_unit_price=0, out_amount=70000, contract_amount=90000)
    assert _row_sales_amount(r2) == 70000

    # no out_amount → contract_amount
    r3 = _make_row(out_fqty=10, order_tax_unit_price=0, out_amount=0, contract_amount=90000)
    assert _row_sales_amount(r3) == 90000


def test_resolve_report_org():
    key, label = _resolve_report_org("huizhou")
    assert key == "huizhou"
    assert label == "惠州伊斯科"

    key, label = _resolve_report_org("guangdong")
    assert key == "guangdong"
    assert label == "广东伊斯科"

    key, label = _resolve_report_org("")
    assert key == "huizhou"

    # Alias
    key, label = _resolve_report_org("广东")
    assert key == "guangdong"

    # Unknown
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match="Unknown org"):
        _resolve_report_org("shanghai")


# ── Customer Sales Report ────────────────────────────────────────────────

def test_customer_sales_month_filter():
    rows = [
        _make_row(fdate="2025-06-01", material_name="C5树脂", out_fqty=100, order_tax_unit_price=1000),
        _make_row(fdate="2025-06-10", material_name="C5树脂", out_fqty=50, order_tax_unit_price=1000),
        _make_row(fdate="2025-07-01", material_name="C5树脂", out_fqty=200, order_tax_unit_price=1000),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}

    result = build_customer_sales_report(rows, org, month="2025-06")
    assert result["month"] == "2025-06"
    assert result["summary"]["total_sales_qty"] == 150
    assert result["summary"]["total_sales_amount_wan"] == 15  # 150*1000 / 10000
    assert result["available_months"] == ["2025-06", "2025-07"]


def test_customer_sales_material_filter():
    rows = [
        _make_row(fdate="2025-06-01", material_name="C5树脂", out_fqty=100, order_tax_unit_price=1000),
        _make_row(fdate="2025-06-10", material_name="间戊二烯", out_fqty=30, order_tax_unit_price=2000),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}

    result = build_customer_sales_report(rows, org, month="2025-06", material="C5树脂")
    assert result["summary"]["total_sales_qty"] == 100


def test_customer_sales_excludes_group_orgs():
    # A normal customer + a group company → group company excluded from share
    rows = [
        _make_row(fdate="2025-06-01", cust_name="青岛橡塑", material_name="C5树脂",
                  out_fqty=60, order_tax_unit_price=1000),
        _make_row(fdate="2025-06-01", cust_name="惠州伊斯科", material_name="C5树脂",
                  out_fqty=40, order_tax_unit_price=1000),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}

    result = build_customer_sales_report(rows, org, month="2025-06")
    # Customer share should only have 青岛橡塑 (not 惠州伊斯科)
    names = {c["name"] for c in result["customer_share"]}
    assert "青岛橡塑" in names
    assert "惠州伊斯科" not in names


def test_customer_sales_yoy():
    # Current month
    rows = [
        _make_row(fdate="2025-06-01", material_name="C5树脂", out_fqty=100, order_tax_unit_price=1000),
        _make_row(fdate="2024-06-01", material_name="C5树脂", out_fqty=50, order_tax_unit_price=1000),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}

    result = build_customer_sales_report(rows, org, month="2025-06")
    # YoY: (100 - 50) / 50 * 100 = 100% increase
    assert result["summary"]["sales_qty_yoy"] == 100.0


def test_customer_sales_customer_share_top5_and_other():
    customers = ["A公司", "B公司", "C公司", "D公司", "E公司", "F公司", "G公司"]
    rows = []
    for i, cust in enumerate(customers):
        rows.append(_make_row(
            fdate="2025-06-01", cust_name=cust, material_name="C5",
            out_fqty=float(100 - i * 10), order_tax_unit_price=1000,
        ))

    org = {"key": "huizhou", "label": "惠州伊斯科"}
    result = build_customer_sales_report(rows, org, month="2025-06")

    share_names = {c["name"] for c in result["customer_share"]}
    assert len(result["customer_share"]) == 6  # top5 + 其他
    assert "其他" in share_names
    assert "A公司" in share_names
    assert "G公司" not in share_names  # is in "其他"


# ── Contract Performance Report ──────────────────────────────────────────

def test_contract_performance_date_range():
    rows = [
        _make_row(fdate="2025-07-01", material_name="C5树脂",
                  contract_fqty=200, contract_amount=1000000, out_fqty=150,
                  order_tax_unit_price=0, out_notoutqty=50, fbillno="BILL-1"),
        _make_row(fdate="2025-07-15", material_name="C5树脂",
                  contract_fqty=100, contract_amount=500000, out_fqty=80,
                  order_tax_unit_price=0, out_notoutqty=20, fbillno="BILL-1"),
        _make_row(fdate="2025-05-01", material_name="C5树脂",
                  contract_fqty=300, contract_amount=800000, out_fqty=300,
                  order_tax_unit_price=0, out_notoutqty=0, fbillno="BILL-2"),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}

    result = build_contract_performance_report(
        rows, org, start_date="2025-07-01", end_date="2025-07-31",
    )

    assert result["start_date"] == "2025-07-01"
    assert result["end_date"] == "2025-07-31"
    s = result["summary"]
    assert s["contract_qty"] == 300  # 200 + 100
    assert s["out_qty"] == 230        # 150 + 80
    assert s["not_out_qty"] == 70     # 300 - 230
    assert s["completion_rate"] == pytest.approx(230 / 300 * 100, 0.01)
    assert s["contract_amount_yi"] == pytest.approx(1500000 / 1e8, 4)
    assert s["unfulfilled_orders"] == 1  # Only BILL-1 has out_notoutqty > 0


def test_contract_performance_unshipped_dedup_and_top15():
    rows = []
    # Same bill, product, type, grade → should dedupe
    for i in range(20):
        qty = 100 + i * 10
        rows.append(_make_row(
            fdate="2025-07-01", fbillno=f"BILL-{i % 5}",  # only 5 unique bills
            material_name=f"产品{i % 3}", product_type="resin", grade="A",
            contract_fqty=qty + 100, contract_amount=1000000,
            out_qty=100, out_notoutqty=qty,  # all >= 30
            order_tax_unit_price=0,
        ))

    org = {"key": "huizhou", "label": "惠州伊斯科"}
    result = build_contract_performance_report(
        rows, org, start_date="2025-07-01", end_date="2025-07-31",
    )

    us = result["unshipped_contracts"]
    # After dedup (bill|mat|ptype|grade), max 5*3=15 keys, capped at 15
    assert len(us) <= 15
    # Sorted by (product, -unshipped_qty)
    if len(us) > 1:
        for i in range(len(us) - 1):
            if us[i]["product"] == us[i + 1]["product"]:
                assert us[i]["unshipped_qty"] >= us[i + 1]["unshipped_qty"]


def test_contract_performance_execution_chart():
    rows = [
        _make_row(fdate="2025-07-01", material_name="C5树脂", product_type="resin", grade="A",
                  contract_fqty=200, out_fqty=150, order_tax_unit_price=0, out_notoutqty=50),
        _make_row(fdate="2025-07-01", material_name="C5树脂", product_type="resin", grade="B",
                  contract_fqty=100, out_fqty=80, order_tax_unit_price=0, out_notoutqty=20),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}
    result = build_contract_performance_report(
        rows, org, start_date="2025-07-01", end_date="2025-07-31",
    )

    ec = result["execution_chart"]
    assert len(ec) >= 1  # C5树脂 aggregates both grades
    total_cq = sum(c["contract_qty"] for c in ec)
    total_oq = sum(c["out_qty"] for c in ec)
    assert total_cq == 300
    assert total_oq == 230


def test_contract_performance_yoy():
    # Current period: 2025-07
    # YoY period: 2024-07
    rows = [
        _make_row(fdate="2025-07-01", contract_fqty=100, contract_amount=500000,
                  out_fqty=80, out_notoutqty=20, fbillno="B1",
                  order_tax_unit_price=0),
        _make_row(fdate="2024-07-01", contract_fqty=200, contract_amount=1000000,
                  out_fqty=150, out_notoutqty=50, fbillno="B2",
                  order_tax_unit_price=0),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}
    result = build_contract_performance_report(
        rows, org, start_date="2025-07-01", end_date="2025-07-31",
    )

    s = result["summary"]
    assert s["qty_yoy"] < 0  # 100 vs 200 = -50%


def test_contract_performance_default_date_range():
    rows = [
        _make_row(fdate="2025-07-28", contract_fqty=100, contract_amount=500000,
                  out_fqty=80, out_notoutqty=20, fbillno="B1",
                  order_tax_unit_price=0),
        _make_row(fdate="2025-06-15", contract_fqty=50, contract_amount=200000,
                  out_fqty=30, out_notoutqty=20, fbillno="B2",
                  order_tax_unit_price=0),
    ]
    org = {"key": "huizhou", "label": "惠州伊斯科"}
    # No explicit date range → defaults to latest 30 days
    result = build_contract_performance_report(rows, org)

    # start_date should be around 06-28 (2025-07-28 minus 30 days)
    assert result["start_date"] >= "2025-06-01"
    assert result["end_date"] == "2025-07-28"


def test_excluded_group_orgs_constants():
    assert "惠州伊斯科" in EXCLUDED_GROUP_ORGS
    assert "ecisco" in EXCLUDED_GROUP_ORGS
    assert len(EXCLUDED_GROUP_ORGS) >= 4
