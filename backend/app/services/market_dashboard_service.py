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


# ── Phase 7 (verbatim port): batch + new endpoint service functions ─

import json as _json
from datetime import date, timedelta

from app.schemas.market_dashboard import (
    RegionalBatchResponse, ForecastBaseBatchResponse,
    EventsResponse, EventItem, FxRateResponse,
)
from app.models.market_dashboard import MATERIAL_FORECAST_IDS


def get_regional_prices_batch(product_ids: list[str]) -> RegionalBatchResponse:
    """Batch version of get_regional_prices. Per-product errors captured."""
    _require_edia()
    items: dict = {}
    errors: dict = {}
    for pid in product_ids:
        try:
            items[pid] = get_regional_prices(pid)
        except Exception as exc:
            logger.warning("EDIA regional-batch failed for %s: %s", pid, exc)
            errors[pid] = str(exc)
    return RegionalBatchResponse(items=items, errors=errors)


def get_forecast_base_batch(
    product_ids: list[str], day: int = 7
) -> ForecastBaseBatchResponse:
    """Read 7-day-ahead forecast prices from v_forecast_price.

    day=1 → max forecast date; day=7 → +6 days; day=30 → +29 days.
    Returns null for products not in MATERIAL_FORECAST_IDS or with no data.
    """
    from app.core.mysql_db import get_mysql_engine
    _require_edia()
    eng = get_mysql_engine()
    out: dict = {}
    if eng is None:
        return ForecastBaseBatchResponse(
            day=day, base_by_product={pid: None for pid in product_ids}
        )
    # Step 1: max forecast date
    try:
        with eng.connect() as conn:
            max_row = conn.execute(text(
                "SELECT MAX(forecast_date) FROM v_forecast_price"
            )).first()
    except Exception as exc:
        logger.warning("EDIA forecast max-date query failed: %s", exc)
        return ForecastBaseBatchResponse(
            day=day, base_by_product={pid: None for pid in product_ids}
        )
    if not max_row or not max_row[0]:
        return ForecastBaseBatchResponse(
            day=day, base_by_product={pid: None for pid in product_ids}
        )
    max_date = max_row[0]
    target_date = max_date + timedelta(days=day - 1)
    # Step 2: per-product forecast
    for pid in product_ids:
        material_ids = MATERIAL_FORECAST_IDS.get(pid, [])
        if not material_ids:
            out[pid] = None
            continue
        try:
            placeholders = ",".join([f":mid_{i}" for i in range(len(material_ids))])
            params: dict = {f"mid_{i}": m for i, m in enumerate(material_ids)}
            params["d"] = target_date
            with eng.connect() as conn:
                rows = conn.execute(text(
                    f"SELECT predicted_price FROM v_forecast_price "
                    f"WHERE material_id IN ({placeholders}) "
                    f"AND forecast_date = :d"
                ), params).fetchall()
        except Exception as exc:
            logger.warning("EDIA forecast query failed for %s: %s", pid, exc)
            out[pid] = None
            continue
        if rows:
            prices = [float(r[0]) for r in rows if r[0] is not None]
            out[pid] = round(sum(prices) / len(prices), 2) if prices else None
        else:
            out[pid] = None
    return ForecastBaseBatchResponse(day=day, base_by_product=out)


def _safe_json_list(raw) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = _json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def get_events_latest(
    hours: int = 168, review_status: str = "approved",
    min_usefulness_score: int = 0,
) -> EventsResponse:
    """Read latest events from the intelligence_events MySQL table.

    Maps the MySQL row directly to the EventItem shape used by EDIA's
    /api/events/latest (1:1 field mapping).
    """
    from app.core.mysql_db import get_mysql_engine
    _require_edia()
    eng = get_mysql_engine()
    if eng is None:
        return EventsResponse(events=[], count=0)
    # Match EDIA's response fields. intelligence_events has these columns:
    #   event_id, headline, event_type, direction, impact_magnitude,
    #   certainty, source_credibility, usefulness_score, relevance_score,
    #   affected_commodities (JSON), causal_chain_hint, source_url,
    #   full_text, raw_quote, relevance_to_c5_c9, source_label, summary,
    #   review_status, detected_at, key_entities (JSON), time_horizon,
    #   geographic_scope
    stmt = text("""
        SELECT event_id, headline, event_type, direction, impact_magnitude,
               certainty, source_credibility, usefulness_score, relevance_score,
               affected_commodities, causal_chain_hint, source_url, full_text,
               raw_quote, relevance_to_c5_c9, source_label, summary,
               detected_at, time_horizon, geographic_scope, key_entities
        FROM intelligence_events
        WHERE review_status = :rs
          AND usefulness_score >= :min_score
          AND detected_at >= NOW() - INTERVAL :hours HOUR
        ORDER BY detected_at DESC
        LIMIT 100
    """)
    try:
        with eng.connect() as conn:
            rows = conn.execute(stmt, {
                "rs": review_status, "min_score": min_usefulness_score,
                "hours": hours,
            }).fetchall()
    except Exception as exc:
        logger.warning("EDIA events query failed: %s", exc)
        return EventsResponse(events=[], count=0)
    events: list[EventItem] = []
    for r in rows:
        events.append(EventItem(
            event_id=str(r[0] or ""),
            headline=str(r[1] or ""),
            event_type=r[2],
            direction=r[3],
            impact_magnitude=float(r[4]) if r[4] is not None else None,
            certainty=r[5],
            source_credibility=r[6],
            usefulness_score=float(r[7]) if r[7] is not None else None,
            relevance_score=float(r[8]) if r[8] is not None else None,
            affected_commodities=_safe_json_list(r[9]),
            causal_chain_hint=r[10],
            source_url=r[11],
            full_text=r[12],
            raw_quote=r[13],
            relevance_to_c5_c9=r[14],
            source_label=r[15],
            summary=r[16],
            detected_at=str(r[17] or ""),
            time_horizon=r[18],
            geographic_scope=r[19],
            key_entities=_safe_json_list(r[20]),
        ))
    return EventsResponse(events=events, count=len(events))


def get_fx_rate() -> FxRateResponse:
    """Static USD-CNY rate for v1. v2 can read from a market data source."""
    return FxRateResponse(
        price=7.20,
        unit="CNY/USD",
        as_of_date=str(date.today()),
        series_product_id="usd_cny",
        series_price_type="spot",
        data_source="static_fallback",
        available=True,
    )
