"""Report data service – ported from EDIA server.py:19342-20072.

Pure-function design: aggregation functions receive normalized row lists,
making them easy to unit-test without a live MySQL connection.
"""
import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text

from app.core.mysql_db import get_mysql_engine, is_edia_reachable

logger = logging.getLogger(__name__)

# ── Constants (from EDIA server.py:19342-19360 + erp_tools.py:50-57) ────

_REPORT_ORG_TABLE_MAP = {
    "huizhou": "erp_v_sale_huizhou",
    "guangdong": "erp_v_sale_guangdong",
}

_ORG_ALIASES = {
    "惠州": "huizhou",
    "广东": "guangdong",
}

_ORG_LABELS = {
    "huizhou": "惠州伊斯科",
    "guangdong": "广东伊斯科",
}

# Huizhou/Guangdong share the same column layout
_UNIFIED_ORG = "huizhou"

EXCLUDED_GROUP_ORGS = {
    "惠州伊斯科新材料科技发展有限公司",
    "惠州伊斯科",
    "伊斯科",
    "ecisco",
    "惠州伊斯科新材料",
}

_EXECUTION_CACHE_TTL = 60  # seconds per org


# ── Helpers ─────────────────────────────────────────────────────────────

def _safe_float(val) -> float:
    try:
        return float(val) if val is not None and val != "" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _week_label(date_str: str) -> str:
    """Convert a date string to a week label like '6.1-6.7'."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        start = dt - timedelta(days=dt.weekday())
        end = start + timedelta(days=6)
        return f"{start.month}.{start.day}-{end.month}.{end.day}"
    except Exception:
        return "unknown"


def _week_monday(date_str: str) -> str:
    """Return the Monday date string for sorting weeks chronologically."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        start = dt - timedelta(days=dt.weekday())
        return start.strftime("%Y-%m-%d")
    except Exception:
        return "9999-12-31"


def _row_sales_amount(row: dict) -> float:
    """Compute the most reliable sales amount for a single execution row."""
    qty = _safe_float(row.get("out_fqty"))
    unit_price = _safe_float(row.get("order_tax_unit_price"))
    out_amount = _safe_float(row.get("out_amount"))
    contract_amount = _safe_float(row.get("contract_amount"))

    if qty > 0 and unit_price > 0:
        return qty * unit_price
    if out_amount > 0:
        return out_amount
    return contract_amount


def _row_amount_source(row: dict) -> str:
    qty = _safe_float(row.get("out_fqty"))
    unit_price = _safe_float(row.get("order_tax_unit_price"))
    out_amount = _safe_float(row.get("out_amount"))
    contract_amount = _safe_float(row.get("contract_amount"))
    if qty > 0 and unit_price > 0:
        return "qty*unit"
    if out_amount > 0:
        return "out_amount"
    return "contract_amount"


def _row_unit_price_source(row: dict) -> str:
    if _safe_float(row.get("ftaxprice_hit_count", 0)) > 0:
        return "ftaxprice"
    if _safe_float(row.get("fprice_hit_count", 0)) > 0:
        return "fprice"
    return "unknown"


# ── Org resolution ──────────────────────────────────────────────────────

def _resolve_report_org(org_input: str) -> tuple[str, str]:
    """Resolve org name → (key, label). Raises 400 for unknown orgs."""
    if not org_input:
        return ("huizhou", _ORG_LABELS["huizhou"])
    key = org_input.strip().lower()
    key = _ORG_ALIASES.get(key, key)
    if key not in _REPORT_ORG_TABLE_MAP:
        allowed = sorted(_REPORT_ORG_TABLE_MAP)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown org '{org_input}'. Allowed: {', '.join(allowed)}",
        )
    return (key, _ORG_LABELS[key])


# ── Row loading + cache ─────────────────────────────────────────────────

_exec_cache: dict[str, tuple[list[dict], float]] = {}
_exec_cache_lock = threading.Lock()


def _norm_row(mapping) -> dict:
    """Convert a DB row (RowMapping or dict) to a plain dict."""
    return dict(mapping)


def _pick(row: dict, *keys, default=""):
    """Pick the first non-null, non-empty value from a list of column aliases.

    Case-insensitive fallback: tries exact key match first, then
    any-case match against the row's actual keys.
    """
    row_keys_lower = {k.lower(): k for k in row}
    for k in keys:
        val = row.get(k)
        if val is not None and val != "":
            return val
        # Case-insensitive fallback
        lower = k.lower()
        actual_key = row_keys_lower.get(lower)
        if actual_key:
            val = row.get(actual_key)
            if val is not None and val != "":
                return val
    return default


def load_contract_execution(org: str) -> tuple[list[dict], dict]:
    """Load all rows from `erp_v_sale_{org}` view and normalize to unified columns.

    Returns (normalized_rows, org_meta_dict).
    Rows are cached per-org for 60 seconds (thread-synchronised).

    Column normalisation logic ported verbatim from EDIA
    _load_contract_execution (server.py:19472-19559).
    """
    org_key, org_label = _resolve_report_org(org)
    now = time.time()

    with _exec_cache_lock:
        cached = _exec_cache.get(org_key)
        if cached and (now - cached[1]) < _EXECUTION_CACHE_TTL:
            cached_rows, _ts = cached
            org_meta = {"key": org_key, "label": org_label}
            return cached_rows, org_meta

    engine = get_mysql_engine()
    table = _REPORT_ORG_TABLE_MAP[org_key]
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT * FROM {table}"))
        raw_rows = [_norm_row(row._mapping) for row in result]

    logger.info(
        "load_contract_execution: org=%s table=%s rows=%d",
        org_key, table, len(raw_rows),
    )

    normalized = []
    for idx, row in enumerate(raw_rows):
        try:
            # Priority: ftaxprice → fprice
            tax_price = _safe_float(
                _pick(row, "ftaxprice", "FTAXPRICE", "order_tax_unit_price")
            )
            plain_price = _safe_float(
                _pick(row, "fprice", "FPRICE")
            )

            # Quantity columns with alias tolerance for different view schemas.
            # Primary source: FDELIQTY (contract total qty), FREMAINOUTQTY (remaining unshipped).
            # Fallback: out_fqty/out_notoutqty if view already has computed columns.
            f_deli_qty = _safe_float(
                _pick(row, "fdeliqty", "FDELIQTY", "contract_fqty", "ORDERQTY")
            )
            f_remain_out_qty = _safe_float(
                _pick(row, "fremainoutqty", "FREMAINOUTQTY", "out_notoutqty")
            )
            # Also try directly if view has computed columns
            raw_out_qty = _safe_float(
                _pick(row, "out_fqty", "OUT_FQTY", "factqty", "FACTQTY")
            )
            raw_not_out = _safe_float(
                _pick(row, "out_notoutqty", "OUT_NOTOUTQTY", "not_out_fqty", "NOT_OUT_FQTY")
            )

            # Compute derived values from view-native columns
            contract_qty = f_deli_qty if f_deli_qty > 0 else _safe_float(
                _pick(row, "fqty_origin", "FQTY_ORIGIN")
            )
            not_out_qty = raw_not_out if raw_not_out > 0 else f_remain_out_qty
            out_qty = raw_out_qty if raw_out_qty > 0 else max(contract_qty - not_out_qty, 0)
            contract_qty = max(contract_qty, out_qty + not_out_qty)

            # Amount: FAMOUNT or computed
            amount = _safe_float(
                _pick(row, "ftaxamount", "FTAXAMOUNT", "famount", "FAMOUNT",
                      "contract_amount", "ORDERAMOUNT")
            )
            invoice_amount = _safe_float(
                _pick(row, "fallamount", "FALLAMOUNT")
            )
            if amount <= 0 and invoice_amount > 0:
                amount = invoice_amount

            # Unit price priority: ftaxprice → fprice → out_amount/qty → amount/qty
            unit_price = tax_price if tax_price > 0 else plain_price
            if unit_price <= 0 and out_qty > 0 and amount > 0:
                unit_price = amount / out_qty

            bill_no = str(_pick(row, "fbillno", "FBILLNO",
                                "bill_no", default=f"{org_key}-{idx}"))
            fdate = str(_pick(row, "date", "plandate", "fdate",
                              "FDATE", "fdeliverydate", "FDELIVERYDATE", default=""))
            fdelivery_date = str(_pick(row, "fdeliverydate", "FDELIVERYDATE",
                                       "date", "plandate", "fdate", "FDATE",
                                       default=fdate))
            material_name = str(_pick(row, "material_name", "product", default="")).strip()
            customer_name = str(_pick(row, "cust_name", "CUST_NAME",
                                      "customer", "partner_name", default="未知")).strip()
            product_type = str(_pick(row, "material_group",
                                     "product_type", "material_type", default="")).strip()
            grade = str(_pick(row, "material_property",
                              "grade", "product_grade", default="")).strip()

            normalized.append({
                "ORG_NAME": org_label,
                "FID": _pick(row, "fid", "FID", default=""),
                "FDATE": fdate,
                "FVALISTARTDATE": str(_pick(row, "fvalistartdate",
                                            "FVALISTARTDATE", default="")),
                "FVALIENDDATE": str(_pick(row, "fvalienddate",
                                          "FVALIENDDATE", default="")),
                "FDELIVERYDATE": fdelivery_date,
                "contract_entry_id": _pick(row, "fentryid", "FENTRYID",
                                           "contract_entry_id", default=""),
                "FBILLNO": bill_no,
                "FNAME": str(_pick(row, "fname", "FNAME", default="销售记录")),
                "CUST_NAME": customer_name,
                "FHEADDELIVERYWAY": str(_pick(row, "fheaddeliveryway",
                                              "FHEADDELIVERYWAY", default="")),
                "product_type": product_type,
                "material_code": _pick(row, "material_code", "MATERIAL_CODE", default=""),
                "material_name": material_name,
                "material_unit": str(_pick(row, "material_unit", "unit", default="吨")),
                "currency": str(_pick(row, "currency", "CURRENCY", default="")),
                "grade": grade,
                "contract_fqty": contract_qty,
                "contract_amount": amount,
                "contract_notdofqty": not_out_qty,
                "ORDERQTY": contract_qty,
                "ORDERAMOUNT": amount,
                "ftaxprice_hit_count": 1 if tax_price > 0 else 0,
                "fprice_hit_count": 1 if tax_price <= 0 and plain_price > 0 else 0,
                "order_tax_unit_price": unit_price,
                "plan_fqty": _safe_float(_pick(row, "plan_qty", "PLAN_QTY")),
                "out_fqty": out_qty,
                "out_amount": amount,
                "out_notoutqty": not_out_qty,
            })
        except Exception:
            logger.warning("load_contract_execution: skip row %d parsing error", idx)

    with _exec_cache_lock:
        _exec_cache[org_key] = (normalized, time.time())

    return normalized, {"key": org_key, "label": org_label}


# ── Customer Sales Report ───────────────────────────────────────────────

def build_customer_sales_report(
    rows: list[dict],
    org_meta: dict,
    month: str = "",
    material: str = "",
    debug: bool = False,
) -> dict:
    """Aggregate customer-sales data from pre-loaded normalized rows.

    Replicates EDIA GET /api/v2/reports/customer-sales logic
    (server.py:19648-19865).
    """
    from datetime import datetime as dt

    # Available months: last 24 distinct months from data
    all_months = sorted({
        r["FDATE"][:7] for r in rows if r.get("FDATE") and len(r["FDATE"]) >= 7
    }, reverse=True)[:24]
    all_months.reverse()  # chronological

    # Available materials
    all_materials = sorted({
        r.get("material_name", "") for r in rows if r.get("material_name", "").strip()
    })

    # Apply month filter
    if month:
        filtered = [r for r in rows if r.get("FDATE", "").startswith(month)]
    else:
        # Default to latest month
        latest = all_months[-1] if all_months else ""
        filtered = [r for r in rows if r.get("FDATE", "").startswith(latest)]
        month = latest

    # Apply material filter
    if material:
        filtered = [r for r in filtered if r.get("material_name", "") == material]

    # -------- Summary --------
    total_qty = sum(_safe_float(r.get("out_fqty")) for r in filtered)
    total_amount = sum(_row_sales_amount(r) for r in filtered)
    total_amount_wan = round(total_amount / 10000, 2)

    # YoY: same month last year
    try:
        y, m = int(month[:4]), int(month[5:7])
        prev_month = f"{y - 1}-{m:02d}"
    except Exception:
        prev_month = ""
    prev_filtered = [r for r in rows if r.get("FDATE", "").startswith(prev_month)]
    if material:
        prev_filtered = [r for r in prev_filtered if r.get("material_name", "") == material]
    prev_qty = sum(_safe_float(r.get("out_fqty")) for r in prev_filtered)
    prev_amount = sum(_row_sales_amount(r) for r in prev_filtered)

    def _yoy(cur, prev):
        return round((cur - prev) / abs(prev) * 100, 1) if prev != 0 else 0

    summary = {
        "total_sales_qty": round(total_qty, 2),
        "total_sales_amount_wan": total_amount_wan,
        "sales_qty_yoy": _yoy(total_qty, prev_qty),
        "sales_amount_yoy": _yoy(total_amount, prev_amount),
    }

    # -------- Customer share (Top5 + Other, excluding group orgs) --------
    cust_qty: dict[str, float] = defaultdict(float)
    for r in filtered:
        name = r.get("CUST_NAME", "").strip()
        if not name or name in EXCLUDED_GROUP_ORGS or name == "未知":
            continue
        cust_qty[name] += _safe_float(r.get("out_fqty"))

    sorted_cust = sorted(cust_qty.items(), key=lambda x: x[1], reverse=True)
    top5 = sorted_cust[:5]
    other_qty = sum(v for _, v in sorted_cust[5:])
    customer_share = [{"name": n, "value": round(v, 2)} for n, v in top5]
    if other_qty > 0:
        customer_share.append({"name": "其他", "value": round(other_qty, 2)})

    # -------- Product growth --------
    product_qty: dict[str, float] = defaultdict(float)
    product_prev_qty: dict[str, float] = defaultdict(float)
    for r in filtered:
        p = r.get("material_name", "").strip()
        if p:
            product_qty[p] += _safe_float(r.get("out_fqty"))
    for r in prev_filtered:
        p = r.get("material_name", "").strip()
        if p:
            product_prev_qty[p] += _safe_float(r.get("out_fqty"))

    product_growth = []
    for p, qty in sorted(product_qty.items(), key=lambda x: x[1], reverse=True):
        prev_q = product_prev_qty.get(p, 0)
        product_growth.append({
            "product": p,
            "qty": round(qty, 2),
            "unit": "吨",
            "yoy": _yoy(qty, prev_q),
        })

    # -------- Weekly charts --------
    # Group rows by week
    week_data: dict[str, list[dict]] = defaultdict(list)
    for r in filtered:
        wl = _week_label(r.get("FDATE", ""))
        if wl == "unknown":
            continue
        week_data[wl].append(r)

    # Sort weeks by Monday date
    sorted_weeks = sorted(
        week_data.keys(),
        key=lambda w: _week_monday(
            (week_data[w][0].get("FDATE", "") if week_data[w] else "")
        ),
    )

    # Price chart: per week, per product price (using unit_price)
    price_chart = []
    for w in sorted_weeks:
        entry = {"week": w}
        for p in sorted(set(
            r.get("material_name", "").strip() for r in week_data[w] if r.get("material_name")
        )):
            p_rows = [r for r in week_data[w] if r.get("material_name", "") == p]
            if p_rows:
                avg_price = sum(_safe_float(r.get("order_tax_unit_price"))
                               for r in p_rows) / len(p_rows)
                entry[p] = round(avg_price, 2)
        price_chart.append(entry)

    # Sales chart: per week, per product qty
    sales_chart = []
    for w in sorted_weeks:
        entry = {"week": w}
        for p in sorted(set(
            r.get("material_name", "").strip() for r in week_data[w] if r.get("material_name")
        )):
            p_qty = sum(_safe_float(r.get("out_fqty"))
                       for r in week_data[w] if r.get("material_name", "") == p)
            entry[p] = round(p_qty, 2)
        sales_chart.append(entry)

    # ── Sales detail (product × customer, per-week columns) ──
    detail_map = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for r in filtered:
        p = r.get("material_name", "").strip()
        c = r.get("CUST_NAME", "").strip()
        wl = _week_label(r.get("FDATE", ""))
        if not p or not c or wl == "unknown":
            continue
        detail_map[(p, c)][wl]["qty"] += _safe_float(r.get("out_fqty"))
        # weighted average price
        qty = _safe_float(r.get("out_fqty"))
        price = _safe_float(r.get("order_tax_unit_price"))
        unit = r.get("material_unit", "吨")
        detail_map[(p, c)][wl]["price"] = price
        detail_map[(p, c)][wl]["unit"] = unit

    sales_detail = []
    for (p, c), weeks in detail_map.items():
        week_items = []
        total_pc_qty = 0
        for w in sorted_weeks:
            wd = weeks.get(w, {"qty": 0, "price": 0})
            week_items.append({
                "week": w,
                "qty": round(wd["qty"], 2),
                "price": round(wd["price"], 2),
            })
            total_pc_qty += wd["qty"]
        sales_detail.append({
            "product": p,
            "customer": c,
            "weeks": week_items,
            "_total": total_pc_qty,
        })

    # Sort by total qty desc, cap at 50
    sales_detail.sort(key=lambda x: x["_total"], reverse=True)
    sales_detail = sales_detail[:50]
    for d in sales_detail:
        del d["_total"]

    # Products list (for chart legends)
    products = sorted(set(
        r.get("material_name", "").strip() for r in filtered if r.get("material_name")
    ))

    result = {
        "month": month,
        "available_months": all_months,
        "available_materials": all_materials,
        "org_name": org_meta["label"],
        "summary": summary,
        "customer_share": customer_share,
        "product_growth": product_growth,
        "weeks": sorted_weeks,
        "price_chart": price_chart,
        "sales_chart": sales_chart,
        "sales_detail": sales_detail,
        "products": products,
    }

    if debug:
        price_source_counts: dict[str, int] = defaultdict(int)
        amount_source_counts: dict[str, int] = defaultdict(int)
        sample_rows = []
        for r in filtered[:200]:
            price_source_counts[_row_unit_price_source(r)] += 1
            amount_source_counts[_row_amount_source(r)] += 1
            sample_rows.append({
                "FDATE": r.get("FDATE", ""),
                "CUST_NAME": r.get("CUST_NAME", ""),
                "material_name": r.get("material_name", ""),
                "out_fqty": _safe_float(r.get("out_fqty")),
                "order_tax_unit_price": _safe_float(r.get("order_tax_unit_price")),
                "out_amount": _safe_float(r.get("out_amount")),
                "contract_amount": _safe_float(r.get("contract_amount")),
                "sales_amount": _row_sales_amount(r),
                "amount_source": _row_amount_source(r),
                "price_source": _row_unit_price_source(r),
            })

        result["debug"] = {
            "price_source_counts": dict(price_source_counts),
            "amount_source_counts": dict(amount_source_counts),
            "sample_rows": sample_rows,
            "sample_limit": 200,
        }

    return result


# ── Contract Performance Report ─────────────────────────────────────────

def build_contract_performance_report(
    rows: list[dict],
    org_meta: dict,
    start_date: str = "",
    end_date: str = "",
    material: str = "",
) -> dict:
    """Aggregate contract-execution KPIs from pre-loaded normalized rows.

    Replicates EDIA GET /api/v2/reports/contract-performance
    (server.py:19868-20072).
    """
    # Default date range: latest 30 days
    all_dates = sorted(r["FDATE"][:10] for r in rows if r.get("FDATE") and len(r.get("FDATE", "")) >= 10)
    if not start_date or not end_date:
        if all_dates:
            end_dt = datetime.strptime(all_dates[-1], "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=30)
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = end_dt.strftime("%Y-%m-%d")
        else:
            start_date = "2025-01-01"
            end_date = "2025-08-31"

    # Available materials
    all_materials = sorted({
        r.get("material_name", "").strip() for r in rows if r.get("material_name", "")
    })

    # Date filter
    filtered = [r for r in rows if start_date <= r.get("FDATE", "")[:10] <= end_date]
    if material:
        filtered = [r for r in filtered if r.get("material_name", "") == material]

    def _yoy(cur, prev):
        return round((cur - prev) / abs(prev) * 100, 1) if prev != 0 else 0

    # Current period aggregates
    total_contract_amount = sum(_safe_float(r.get("contract_amount")) for r in filtered)
    total_contract_qty = sum(_safe_float(r.get("contract_fqty")) for r in filtered)
    total_out_qty = sum(_safe_float(r.get("out_fqty")) for r in filtered)
    total_not_out_qty = max(total_contract_qty - total_out_qty, 0)

    unfulfilled_orders = len(set(
        r.get("FBILLNO", "") for r in filtered
        if _safe_float(r.get("out_notoutqty")) > 0
    ))
    completion_rate = round(
        total_out_qty / total_contract_qty * 100, 2
    ) if total_contract_qty > 0 else 0

    # Invoice: sum of contract_amount where out_qty > 0 as proxy
    invoice_amount = sum(
        _safe_float(r.get("contract_amount")) for r in filtered
        if _safe_float(r.get("out_fqty")) > 0
    )
    uninvoiced_amount = total_contract_amount - invoice_amount
    invoice_amount_wan = round(invoice_amount / 10000, 2)
    uninvoiced_amount_wan = round(uninvoiced_amount / 10000, 2)

    # Previous period (YoY: shift entire range by 365 days)
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d")
        prev_start = (sd - timedelta(days=365)).strftime("%Y-%m-%d")
        prev_end = (ed - timedelta(days=365)).strftime("%Y-%m-%d")
    except Exception:
        prev_start = prev_end = ""
    prev_filtered = [r for r in rows if prev_start <= r.get("FDATE", "")[:10] <= prev_end]
    if material:
        prev_filtered = [r for r in prev_filtered if r.get("material_name", "") == material]

    prev_total_amount = sum(_safe_float(r.get("contract_amount")) for r in prev_filtered)
    prev_total_qty = sum(_safe_float(r.get("contract_fqty")) for r in prev_filtered)
    prev_out_qty = sum(_safe_float(r.get("out_fqty")) for r in prev_filtered)
    prev_not_out = max(prev_total_qty - prev_out_qty, 0)
    prev_unfulfilled = len(set(
        r.get("FBILLNO", "") for r in prev_filtered
        if _safe_float(r.get("out_notoutqty")) > 0
    ))
    prev_completion = round(
        prev_out_qty / prev_total_qty * 100, 2
    ) if prev_total_qty > 0 else 0
    prev_invoice = sum(
        _safe_float(r.get("contract_amount")) for r in prev_filtered
        if _safe_float(r.get("out_fqty")) > 0
    )
    prev_uninvoiced = prev_total_amount - prev_invoice

    summary = {
        "contract_amount_yi": round(total_contract_amount / 1e8, 4),
        "unfulfilled_orders": unfulfilled_orders,
        "completion_rate": completion_rate,
        "contract_qty": round(total_contract_qty, 2),
        "out_qty": round(total_out_qty, 2),
        "not_out_qty": round(total_not_out_qty, 2),
        "invoice_amount_wan": invoice_amount_wan,
        "uninvoiced_amount_wan": uninvoiced_amount_wan,
        "amount_yoy": _yoy(total_contract_amount, prev_total_amount),
        "qty_yoy": _yoy(total_contract_qty, prev_total_qty),
        "unfulfilled_yoy": _yoy(unfulfilled_orders, prev_unfulfilled),
        "completion_rate_yoy": _yoy(completion_rate, prev_completion),
        "contract_qty_yoy": _yoy(total_contract_qty, prev_total_qty),
        "out_qty_yoy": _yoy(total_out_qty, prev_out_qty),
        "not_out_qty_yoy": _yoy(total_not_out_qty, prev_not_out),
        "invoice_yoy": _yoy(invoice_amount, prev_invoice),
        "uninvoiced_yoy": _yoy(uninvoiced_amount, prev_uninvoiced),
    }

    # Unshipped contracts: not_out_qty >= 30, dedupe by (bill|mat|ptype|grade),
    # top 15 by qty → re-sort by (product, -qty)
    unshipped_raw = [
        r for r in filtered if _safe_float(r.get("out_notoutqty")) >= 30
    ]
    deduped: dict[str, dict] = {}
    for r in unshipped_raw:
        key = "|".join([
            r.get("FBILLNO", ""),
            r.get("material_name", ""),
            r.get("product_type", ""),
            r.get("grade", ""),
        ])
        if key not in deduped:
            deduped[key] = {
                "product": r.get("material_name", "").strip(),
                "product_type": r.get("product_type", "").strip(),
                "bill_no": r.get("FBILLNO", ""),
                "customer": r.get("CUST_NAME", "").strip(),
                "delivery_date": r.get("FDELIVERYDATE", "")[:10],
                "contract_qty": _safe_float(r.get("contract_fqty")),
                "unshipped_qty": _safe_float(r.get("out_notoutqty")),
            }

    sorted_by_qty = sorted(
        deduped.values(), key=lambda x: x["unshipped_qty"], reverse=True
    )[:15]
    unshipped_contracts = sorted(
        sorted_by_qty,
        key=lambda x: (x["product"], -x["unshipped_qty"]),
    )

    # Execution chart: by (material_name × product_type × grade) group
    exec_groups = defaultdict(lambda: {"contract_qty": 0.0, "out_qty": 0.0})
    for r in filtered:
        key = "|".join([
            r.get("material_name", "").strip(),
            r.get("product_type", "").strip(),
            r.get("grade", "").strip(),
        ])
        exec_groups[key]["contract_qty"] += _safe_float(r.get("contract_fqty"))
        exec_groups[key]["out_qty"] += _safe_float(r.get("out_fqty"))

    execution_chart = []
    for key, vals in sorted(exec_groups.items(),
                            key=lambda x: x[1]["contract_qty"], reverse=True):
        cq = vals["contract_qty"]
        oq = vals["out_qty"]
        rate = round(oq / cq * 100, 2) if cq > 0 else 0
        # Use first key part as product label
        parts = key.split("|")
        product_label = parts[0]
        execution_chart.append({
            "product": product_label,
            "contract_qty": round(cq, 2),
            "out_qty": round(oq, 2),
            "execution_rate": rate,
        })

    return {
        "start_date": start_date,
        "end_date": end_date,
        "available_materials": all_materials,
        "org_name": org_meta["label"],
        "summary": summary,
        "unshipped_contracts": unshipped_contracts,
        "execution_chart": execution_chart,
    }
