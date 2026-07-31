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

    Uses raw SQL (SELECT * FROM table) + result.keys() to find the value
    column index — mirrors EDIA's market_db.py approach. The first column
    is always the date index (EDIA's Excel-ingestion convention).
    """
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if cfg is None:
        return []
    eng = get_mysql_engine()
    if eng is None:
        return []
    value_col = cfg["value_col"]
    table_name = cfg["table"]
    stmt = text(f"SELECT * FROM `{table_name}`")
    out: list[tuple] = []
    with eng.connect() as conn:
        result = conn.execute(stmt)
        rows = result.fetchall()
        cols = list(result.keys())
    if not cols or value_col not in cols:
        logger.warning(
            "EDIA column %r not in table %r (cols=%s)",
            value_col, table_name, cols[:6],
        )
        return []
    date_idx = 0
    value_idx = cols.index(value_col)
    for r in rows:
        raw_date = str(r[date_idx] or "")
        raw_price = r[value_idx]
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
            data_source_detail=f"EDIA MySQL: {PRODUCT_TABLE_MAP[pid]['table']}",
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
