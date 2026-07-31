"""Read-only queries against EDIA's MySQL warehouse.

v1 scope: snapshot + history per-product using EDIA's table mapping.
change_pct is computed from the last two history points (simpler than
EDIA's _compute_market_snapshot, which depends on ERP sales data +
execution prices not present in our read-only mirror).
"""
import logging
from typing import Optional
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.core.mysql_db import get_mysql_engine, is_edia_reachable
from app.models.market_dashboard import (
    PRODUCT_TABLE_MAP, PRODUCT_GROUP_MAP, PRODUCT_LABELS, get_product_table,
    DATE_COL, PRICE_COL, SUPPLIER_COL,
)
from app.schemas.market_dashboard import MarketSnapshot, PriceHistoryPoint

logger = logging.getLogger(__name__)


def is_edia_enabled() -> bool:
    return bool(settings.EDIA_MYSQL_URL)


def is_edia_reachable_cached() -> bool:
    return is_edia_reachable()


def _require_edia():
    """Raise 503 if EDIA is not configured or not reachable."""
    if not settings.EDIA_MYSQL_URL:
        raise HTTPException(status_code=503, detail="edia_unavailable")
    if not is_edia_reachable_cached():
        raise HTTPException(status_code=503, detail="edia_unreachable")


def _read_product_history_rows(product_id: str) -> list[tuple]:
    """Return [(date_str, price_float), ...] from EDIA's table for a product.

    Uses the unified EDIA schema: filters by 厂商名supplier_name to get
    the specific price series, reads 日期Time + 价格price columns.
    """
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if cfg is None:
        return []
    eng = get_mysql_engine()
    if eng is None:
        return []
    supplier = cfg["supplier_name"]
    table_name = cfg["table"]
    stmt = text(
        f"SELECT `{DATE_COL}` AS d, `{PRICE_COL}` AS p "
        f"FROM `{table_name}` "
        f"WHERE `{SUPPLIER_COL}` = :supplier "
        f"ORDER BY `{DATE_COL}` ASC"
    )
    out: list[tuple] = []
    with eng.connect() as conn:
        result = conn.execute(stmt, {"supplier": supplier})
        for row in result:
            raw_date = str(row[0] or "")
            raw_price = row[1]
            if raw_price is None:
                continue
            try:
                price_val = float(raw_price)
            except (TypeError, ValueError):
                continue
            date_str = raw_date.replace("/", "-")[:10]
            out.append((date_str, round(price_val, 2)))
    return out


def get_price_history(product_id: str, days: int = 30) -> list[PriceHistoryPoint]:
    """Return daily price points for the last N days (default 30)."""
    _require_edia()
    rows = _read_product_history_rows(product_id)
    if not rows:
        return []
    cutoff = (date.today() - timedelta(days=max(days, 1))).isoformat()
    out: list[PriceHistoryPoint] = []
    for d, p in rows:
        if d < cutoff:
            continue
        out.append(PriceHistoryPoint(date=d, price=p))
    return out


def get_market_snapshot(group: Optional[str] = None) -> list[MarketSnapshot]:
    """Return one MarketSnapshot per product in the requested group.

    group=None returns all 12 products (used by /overview).
    Products without a table mapping return price=None + a note.
    """
    _require_edia()
    if group is None:
        product_ids: list[str] = []
        for grp, ids in PRODUCT_GROUP_MAP.items():
            product_ids.extend(ids)
    else:
        product_ids = PRODUCT_GROUP_MAP.get(group, [])

    snapshots: list[MarketSnapshot] = []
    for pid in product_ids:
        labels = PRODUCT_LABELS.get(pid, {})
        name = labels.get("label_en", pid)
        rows = _read_product_history_rows(pid) if pid in PRODUCT_TABLE_MAP else []
        if not rows:
            snapshots.append(MarketSnapshot(
                id=pid,
                name=name,
                group=labels.get("group", group or "unknown"),
                price=None,
                date=None,
                change_pct=None,
                note=("table mapping not configured in v1"
                      if pid not in PRODUCT_TABLE_MAP else "no rows in EDIA table"),
            ))
            continue
        last_date, last_price = rows[-1]
        prev_price = rows[-2][1] if len(rows) > 1 else None
        change_pct = (
            round((last_price - prev_price) / prev_price * 100, 2)
            if prev_price else 0.0
        )
        # 7-day window for weekly_change_pct (calendar-date based)
        weekly_rows = [r for r in rows if _within_7d(r[0], last_date)]
        weekly_prev = weekly_rows[0][1] if len(weekly_rows) > 1 else None
        weekly_change = (
            round((last_price - weekly_prev) / weekly_prev * 100, 2)
            if weekly_prev else None
        )
        snapshots.append(MarketSnapshot(
            id=pid,
            name=name,
            group=labels.get("group", group or "unknown"),
            price=last_price,
            date=last_date,
            change_pct=change_pct,
            weekly_change_pct=weekly_change,
            daily_gap_value=round(last_price - prev_price, 2) if prev_price else None,
            daily_reference_date=(rows[-2][0] if len(rows) > 1 else None),
            weekly_gap_value=round(last_price - weekly_prev, 2) if weekly_prev else None,
            weekly_reference_date=(weekly_rows[0][0] if len(weekly_rows) > 0 else None),
            data_source_detail=f"EDIA MySQL: {PRODUCT_TABLE_MAP[pid]['table']} / {PRODUCT_TABLE_MAP[pid]['supplier_name']}",
        ))
    return snapshots


def _within_7d(d_str: str, ref_date_str: str) -> bool:
    """Return True if d_str is within 7 calendar days before ref_date_str."""
    try:
        d = date.fromisoformat(d_str)
        ref = date.fromisoformat(ref_date_str)
    except ValueError:
        return False
    return (ref - timedelta(days=7)) <= d <= ref


# ── Phase E: regional + news (v1 simplified) ──────────────────────────

from app.schemas.market_dashboard import (
    RegionalPriceData, RegionalPriceRegion, NewsListResponse,
)

# Supplier-name fragments that look like a regional price.
_REGIONAL_TOKENS = ("华东", "华南", "华中", "华北", "上海", "East China", "South China", "North China")


def get_regional_prices(product_id: str) -> RegionalPriceData:
    """v1 simplified regional payload: one region per matching supplier_name.

    Scans distinct 厂商名supplier_name values for regional-looking names
    (华东市场价, 华南市场价, 上海石化, etc.) and returns the latest
    price for each. No spread or cost-floor analysis.
    """
    _require_edia()
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    eng = get_mysql_engine()
    if cfg is None or eng is None:
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note="table mapping not configured in v1",
        )
    table_name = cfg["table"]
    # Get distinct regional suppliers
    distinct_stmt = text(
        f"SELECT DISTINCT `{SUPPLIER_COL}` FROM `{table_name}` "
        f"WHERE `{SUPPLIER_COL}` IS NOT NULL"
    )
    region_suppliers: list[str] = []
    with eng.connect() as conn:
        for row in conn.execute(distinct_stmt):
            name = row[0]
            if name and any(tok in str(name) for tok in _REGIONAL_TOKENS):
                region_suppliers.append(str(name))
    if not region_suppliers:
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note="no regional suppliers detected in EDIA table",
        )
    regions: list[RegionalPriceRegion] = []
    for supplier in region_suppliers:
        stmt = text(
            f"SELECT `{DATE_COL}` AS d, `{PRICE_COL}` AS p "
            f"FROM `{table_name}` "
            f"WHERE `{SUPPLIER_COL}` = :supplier "
            f"AND `{PRICE_COL}` IS NOT NULL "
            f"ORDER BY `{DATE_COL}` DESC LIMIT 1"
        )
        try:
            with eng.connect() as conn:
                row = conn.execute(stmt, {"supplier": supplier}).first()
        except Exception as exc:
            logger.warning("EDIA regional query failed for %s.%s: %s",
                           product_id, supplier, exc)
            continue
        if not row or row[1] is None:
            continue
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        regions.append(RegionalPriceRegion(
            price_type=supplier,
            label=supplier,
            is_internal=False,
            price=round(price, 2),
            change_pct=None,
            date=str(row[0])[:10].replace("/", "-") if row[0] else "",
            days_stale=0,
        ))
    return RegionalPriceData(
        product_id=product_id,
        regions=regions,
        note="v1 simplified: no spread/cost-floor analysis",
    )


def get_news_events(group=None, limit=18) -> NewsListResponse:
    """v1 returns an empty list with an explanatory note.

    EDIA sources news from /api/events/latest — an AI-detected market-event
    pipeline stored in a separate system, not in the
    aipdp_data_warehouse_prod warehouse we mirror. Replicating that pipeline
    is out of scope for v1; the frontend renders an info banner instead.
    """
    _require_edia()
    return NewsListResponse(
        items=[],
        note=(
            "Industry news requires EDIA's AI event pipeline "
            "(not part of this v1 read-only mirror)."
        ),
    )
