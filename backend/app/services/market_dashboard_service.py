"""Read-only queries against EDIA's MySQL warehouse.

v1 scope: snapshot + history per-product using EDIA's table mapping.
change_pct is computed from the last two history points (simpler than
EDIA's _compute_market_snapshot, which depends on ERP sales data +
execution prices not present in our read-only mirror).
"""
import logging
import threading
import time
from typing import Optional
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.core.mysql_db import get_mysql_engine, is_edia_reachable
from app.models.market_dashboard import (
    PRODUCT_TABLE_MAP, PRODUCT_GROUP_MAP, PRODUCT_LABELS, get_product_table,
    get_md_lz_regional_prefix,
    DATE_COL, PRICE_COL, SUPPLIER_COL,
)
from app.schemas.market_dashboard import (
    GradeKpiData, GradeKpiRow, GradeKpiSummary, MarketSnapshot, PriceHistoryPoint,
)
from app.services.erp.kpi import (
    _PRODUCT_MATERIAL_NAMES, _material_name_params, _parse_ref_date as _parse_date,
)

logger = logging.getLogger(__name__)


# ── In-memory response cache ─────────────────────────────────────────────
# Brief 30-second TTL cache so the chemical-market overview doesn't hammer
# MySQL (10.10.10.49 — WAN). Concurrent requests share the cached payload.
# The cache is single-process; multi-worker deployments would each have a
# cache. Stale-after-expiry semantics; explicit `?no_cache=1` bypasses.
_OVERVIEW_CACHE_TTL_MS = 30_000
_overview_cache_lock = threading.Lock()
_overview_cache: dict = {}  # key -> {"value": ..., "cached_at_ms": ...}


def _cache_get(key: str, ttl_ms: int = _OVERVIEW_CACHE_TTL_MS):
    """Return (value, age_ms) or (None, None) if missing/expired."""
    with _overview_cache_lock:
        hit = _overview_cache.get(key)
        if not hit:
            return None, None
        age = int(time.time() * 1000) - hit["cached_at_ms"]
        if age > ttl_ms:
            _overview_cache.pop(key, None)
            return None, None
        return hit["value"], age


def _cache_put(key: str, value) -> None:
    with _overview_cache_lock:
        _overview_cache[key] = {
            "value": value,
            "cached_at_ms": int(time.time() * 1000),
        }


def _cache_invalidate_prefix(prefix: str) -> int:
    """Drop cache entries whose key starts with `prefix` (used on Refresh)."""
    with _overview_cache_lock:
        keys = [k for k in _overview_cache if k.startswith(prefix)]
        for k in keys:
            _overview_cache.pop(k, None)
    return len(keys)


def _overview_product_ids() -> list[str]:
    """All product_ids the overview page surfaces (mapped + unmapped)."""
    out: list[str] = []
    for ids in PRODUCT_GROUP_MAP.values():
        out.extend(ids)
    return out


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


def _schema_for(product_id: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """Return (date_col, price_col, supplier_col, supplier_filter) for a product.

    Some EDIA tables (lz_v_*_data, sale_erp_v_*_data) use different column
    names than the default 日期Time/价格price/厂商名supplier_name schema.
    PRODUCT_TABLE_MAP entries can override the column names via
    `date_col` / `price_col` / `supplier_col`. A `supplier_col` of None
    means the table has no supplier column; pass-through aggregations
    only. `supplier_name == "__all__"` means "no filter" for the supplier
    column (or no supplier column at all).
    """
    cfg = PRODUCT_TABLE_MAP.get(product_id, {})

    # date_col / price_col fall back to default EDIA schema unless
    # explicitly overridden. (Empty string is treated as missing.)
    date_col = cfg.get("date_col") or DATE_COL
    price_col = cfg.get("price_col") or PRICE_COL

    # supplier_col is OPTIONAL: a missing entry is fine (fallback), but an
    # EXPLICIT None means "table has no supplier column". Use the sentinel
    # pattern to distinguish absence vs None.
    _MISSING = object()
    raw = cfg.get("supplier_col", _MISSING)
    if raw is _MISSING:
        supplier_col = SUPPLIER_COL
    elif raw is None:
        supplier_col = None
    elif not raw:
        supplier_col = SUPPLIER_COL
    else:
        supplier_col = raw

    return (
        date_col,
        price_col,
        supplier_col,
        cfg.get("supplier_name"),
    )


def _read_md_lz_history_rows(
    primary_cfg: dict, eng,
) -> list[tuple]:
    """Read history rows from the md_t_lz_price primary source.

    Returns [(date_str, price_float), ...]. The FMATERIAL_NAME is matched
    exactly (the primary config encodes the full "{product}-{supplier}"
    string). FTAXPRICE is varchar — we coerce defensively, dropping any
    non-numeric values (e.g., "F7", "NaN") so they don't poison the
    price series.
    """
    table_name = primary_cfg["table"]
    name_pattern = primary_cfg["name_pattern"]
    date_col = primary_cfg["date_col"]
    price_col = primary_cfg["price_col"]
    material_col = primary_cfg["material_col"]

    stmt = text(
        f"SELECT `{date_col}` AS d, `{price_col}` AS p "
        f"FROM `{table_name}` "
        f"WHERE `{material_col}` = :name "
        f"ORDER BY `{date_col}` ASC"
    )
    out: list[tuple] = []
    with eng.connect() as conn:
        for row in conn.execute(stmt, {"name": name_pattern}):
            raw_date = str(row[0] or "")
            raw_price = row[1]
            if raw_price is None:
                continue
            # FTAXPRICE is varchar — drop non-numeric sentinels like "F7"
            try:
                price_val = float(raw_price)
            except (TypeError, ValueError):
                continue
            if price_val != price_val:  # NaN check
                continue
            date_str = raw_date.replace("/", "-")[:10]
            out.append((date_str, round(price_val, 2)))
    return out


def read_product_history_rows(product_id: str) -> list[tuple]:
    """Public alias of _read_product_history_rows for the analyst pack."""
    return _read_product_history_rows(product_id)


def _read_product_history_rows(product_id: str) -> list[tuple]:
    """Return [(date_str, price_float), ...] from EDIA's table for a product.

    Tries the `primary` source (md_t_lz_price) first, then falls back to
    the legacy long-form table. The fallback is silent if the primary
    returns rows; the long-form table only runs when the primary has
    zero rows (so we never pay the cost of a dual query).
    """
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if cfg is None:
        return []
    eng = get_mysql_engine()
    if eng is None:
        return []

    # 1. Primary source (md_t_lz_price, LIVE) — short-circuits if it has data.
    primary = cfg.get("primary")
    if primary and primary.get("table") == "md_t_lz_price":
        try:
            primary_rows = _read_md_lz_history_rows(primary, eng)
        except Exception as exc:
            logger.warning(
                "md_t_lz_price primary read failed for %s: %s",
                product_id, exc,
            )
            primary_rows = []
        if primary_rows:
            return primary_rows
        logger.info(
            "md_t_lz_price returned 0 rows for %s; falling back to legacy %s",
            product_id, cfg.get("table"),
        )

    # 2. Fallback to legacy long-form table.
    supplier_filter = cfg["supplier_name"]
    table_name = cfg["table"]
    date_col, price_col, supplier_col, _ = _schema_for(product_id)

    if supplier_filter and supplier_filter != "__all__":
        stmt = text(
            f"SELECT `{date_col}` AS d, `{price_col}` AS p "
            f"FROM `{table_name}` "
            f"WHERE `{supplier_col}` = :supplier "
            f"ORDER BY `{date_col}` ASC"
        )
        params = {"supplier": supplier_filter}
    else:
        stmt = text(
            f"SELECT `{date_col}` AS d, `{price_col}` AS p "
            f"FROM `{table_name}` "
            f"ORDER BY `{date_col}` ASC"
        )
        params = {}

    out: list[tuple] = []
    with eng.connect() as conn:
        result = conn.execute(stmt, params)
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
    """Return daily price points for the last N days (default 30).

    Each point also carries ``volume`` (per-day sales volume in tons) when the
    product has an ERP material mapping; otherwise ``volume`` stays ``None``.
    """
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

    # Attach per-day sales volume (ERP-mapped products only). Brent/Naphtha
    # have no material mapping, so their volume stays None.
    if out:
        daily = _fetch_daily_sales(product_id, out[0].date, out[-1].date)
        if daily:
            for pt in out:
                pt.volume = daily.get(pt.date)
    return out


def _fetch_daily_sales(product_id: str, start_date: str, end_date: str) -> dict[str, float]:
    """Return ``{YYYY-MM-DD: volume_tons}`` per-day sales for a product.

    Groups ``erp_v_sale_orderentry.FDELIQTY`` by ``DATE(PLANDATE)`` over
    ``[start_date, end_date]``. Returns an empty dict for products without an
    ERP material mapping or on any error (caller degrades gracefully).
    """
    material_names = _PRODUCT_MATERIAL_NAMES.get(product_id, [])
    if not material_names:
        return {}
    eng = get_mysql_engine()
    if eng is None:
        return {}
    name_ph, name_params = _material_name_params(material_names)
    try:
        with eng.connect() as conn:
            params = dict(name_params)
            params["start_date"] = start_date
            params["end_date"] = end_date
            rows = conn.execute(text(
                f"SELECT DATE(PLANDATE) AS d, SUM(COALESCE(FDELIQTY, 0)) AS vol "
                f"FROM erp_v_sale_orderentry "
                f"WHERE material_name IN ({name_ph}) "
                f"  AND PLANDATE IS NOT NULL "
                f"  AND DATE(PLANDATE) >= :start_date AND DATE(PLANDATE) <= :end_date "
                f"GROUP BY DATE(PLANDATE)"
            ), params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily-sales query failed for %s: %s", product_id, exc)
        return {}

    out: dict[str, float] = {}
    for row in rows:
        d = row[0]
        v = row[1]
        if d is None or v is None:
            continue
        d_str = d if isinstance(d, str) else str(d)[:10]
        try:
            out[d_str] = round(float(v), 2)
        except (TypeError, ValueError):
            continue
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
        # 7-day high/low from the same window (no extra query needed).
        weekly_prices = [p for _, p in weekly_rows]
        price_7d_high = round(max(weekly_prices), 2) if weekly_prices else None
        price_7d_low = round(min(weekly_prices), 2) if weekly_prices else None
        # 7-day sales volume + change (ERP-mapped products only). Any
        # unexpected error here must degrade to None, never abort the snapshot.
        try:
            sales_7d_tons, sales_7d_change_pct = _fetch_sales_7d(pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sales-7d lookup raised for %s: %s", pid, exc)
            sales_7d_tons, sales_7d_change_pct = None, None
        # Data source provenance: prefer the md_t_lz_price primary over
        # the legacy long table so users see where the live price came from.
        cfg = PRODUCT_TABLE_MAP[pid]
        primary = cfg.get("primary")
        if primary and primary.get("table") == "md_t_lz_price":
            source_detail = (
                f"EDIA MySQL: md_t_lz_price (LIVE) / {primary['name_pattern']}"
            )
        else:
            source_detail = (
                f"EDIA MySQL: {cfg['table']} / {cfg['supplier_name']}"
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
            data_source_detail=source_detail,
            price_7d_high=price_7d_high,
            price_7d_low=price_7d_low,
            sales_7d_tons=sales_7d_tons,
            sales_7d_change_pct=sales_7d_change_pct,
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


def _fetch_sales_7d(product_id: str) -> tuple[Optional[float], Optional[float]]:
    """Return (sales_7d_tons, sales_7d_change_pct) for an ERP-mapped product.

    Queries ``erp_v_sale_orderentry`` with a single combined SQL statement that
    conditionally aggregates the current 7-day window vs. the prior 7-day window
    (one round-trip per product), mirroring the direct EDIA MySQL pattern used by
    ``get_grade_kpis_batch``.

    Returns ``(None, None)`` on any error or missing data so the overview card
    grid degrades gracefully.
    """
    material_names = _PRODUCT_MATERIAL_NAMES.get(product_id, [])
    if not material_names:
        return None, None

    eng = get_mysql_engine()
    if eng is None:
        return None, None

    name_ph, name_params = _material_name_params(material_names)

    # Reference date = latest delivery date in the warehouse (same convention as
    # get_grade_kpis_batch / ErpKpiService). Fall back to today on any error.
    ref_date = date.today()
    try:
        with eng.connect() as conn:
            ref_row = conn.execute(text(
                "SELECT MAX(DATE(PLANDATE)) FROM erp_v_sale_orderentry "
                "WHERE PLANDATE IS NOT NULL"
            )).first()
            if ref_row and ref_row[0] is not None:
                ref_date = _parse_date(ref_row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("sales-7d ref-date query failed for %s: %s", product_id, exc)

    ref_str = ref_date.isoformat()
    # Current window: [ref_date - 6d, ref_date] (7 days inclusive).
    # Prior window:   [ref_date - 13d, ref_date - 7d] (the 7 days before).
    curr_start = (ref_date - timedelta(days=6)).isoformat()
    prev_start = (ref_date - timedelta(days=13)).isoformat()
    prev_end = (ref_date - timedelta(days=7)).isoformat()

    try:
        with eng.connect() as conn:
            params = dict(name_params)
            params["curr_start"] = curr_start
            params["ref_str"] = ref_str
            params["prev_start"] = prev_start
            params["prev_end"] = prev_end
            row = conn.execute(text(
                f"SELECT "
                f"  SUM(CASE WHEN PLANDATE IS NOT NULL "
                f"         AND DATE(PLANDATE) >= :curr_start "
                f"         AND DATE(PLANDATE) <= :ref_str "
                f"      THEN COALESCE(FDELIQTY, 0) ELSE 0 END) AS curr_vol, "
                f"  SUM(CASE WHEN PLANDATE IS NOT NULL "
                f"         AND DATE(PLANDATE) >= :prev_start "
                f"         AND DATE(PLANDATE) <= :prev_end "
                f"      THEN COALESCE(FDELIQTY, 0) ELSE 0 END) AS prev_vol "
                f"FROM erp_v_sale_orderentry "
                f"WHERE material_name IN ({name_ph})"
            ), params).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sales-7d query failed for %s: %s", product_id, exc)
        return None, None

    if row is None:
        return None, None

    curr_vol = float(row[0]) if row[0] is not None else None
    prev_vol = float(row[1]) if row[1] is not None else None

    sales_7d_tons = round(curr_vol, 2) if curr_vol is not None else None

    sales_7d_change_pct = None
    if curr_vol is not None and prev_vol is not None and prev_vol > 0:
        sales_7d_change_pct = round((curr_vol - prev_vol) / prev_vol * 100, 2)

    return sales_7d_tons, sales_7d_change_pct


# ── Phase E: regional + news (v1 simplified) ──────────────────────────

from app.schemas.market_dashboard import (
    RegionalPriceData, RegionalPriceRegion, NewsListResponse,
)

# Supplier-name fragments that match a regional price (华东/华南/华中/华北
# /上海/广州/茂名/扬子/中石化/北京/天津/山东/青岛/武汉/巴陵)
_REGIONAL_TOKENS = (
    "华东", "华南", "华中", "华北", "上海", "广州", "茂名", "扬子", "中石化",
    "中韩", "广州石化", "茂名石化", "上海石化", "北京", "天津", "山东",
    "青岛", "武汉", "巴陵", "East China", "South China", "North China",
    "Yangzi",
)

# Suppliers that represent a national / single-source benchmark rather than
# a regional market reference. Their rows are still returned (so users see
# the headline price), but tagged so the UI can group them distinctly.
_NATIONAL_AVERAGE_TOKENS = ("均价", "巴陵", "Brent", "布伦特", "CFR", "挂牌")


def _read_md_lz_regional_rows(
    primary_cfg: dict, prefix: str, eng,
    supplier_whitelist: Optional[list[str]] = None,
) -> list[RegionalPriceRegion]:
    """Read per-supplier latest prices from md_t_lz_price for a product.

    Enumerates FMATERIAL_NAME values for the product and returns one row
    per name with the latest FDATE + FTAXPRICE. The supplier (suffix
    after the first '-') drives the row label.

    `supplier_whitelist` (optional) — list of bare supplier names from
    the legacy LZ/long-form config (e.g. ["布伦特"], ["裂解C5均价", "扬子裂解C5", ...]).
    When provided, ONLY the FMATERIAL_NAME values whose suffix matches
    one of these suppliers (after joining `prefix + supplier`) are
    returned. This prevents products that share a prefix (e.g. Brent,
    Naphtha, Cracked C5 all live under `裂解C5-`) from showing each
    other's regional series.

    When the whitelist is empty/absent, falls back to a LIKE prefix
    scan so products without a curated list surface every supplier
    the Longzhong feed publishes for them.
    """
    table_name = primary_cfg["table"]
    material_col = primary_cfg["material_col"]
    date_col = primary_cfg["date_col"]
    price_col = primary_cfg["price_col"]

    # Build the candidate-name set.
    # - With whitelist: full FMATERIAL_NAME = prefix + supplier (e.g. "裂解C5-布伦特").
    #   The whitelist may already contain a fully-qualified name (some
    #   configs use "裂解C5-裂解C5均价" as-is) — those are passed through.
    # - Without whitelist: LIKE prefix scan.
    with eng.connect() as conn:
        if supplier_whitelist:
            cleaned: list[str] = []
            for s in supplier_whitelist:
                if not s:
                    continue
                s = str(s).strip()
                if not s:
                    continue
                # If the whitelist entry is already fully-qualified, keep it.
                if s.startswith(prefix):
                    cleaned.append(s)
                else:
                    cleaned.append(prefix + s)
            if not cleaned:
                return []
            # De-dup while preserving order; one round-trip via IN clause.
            seen: set[str] = set()
            unique: list[str] = []
            for s in cleaned:
                if s not in seen:
                    seen.add(s)
                    unique.append(s)
            # Build a parameterized IN clause.
            placeholders = ", ".join(f":n{i}" for i in range(len(unique)))
            params = {f"n{i}": v for i, v in enumerate(unique)}
            stmt = text(
                f"SELECT DISTINCT `{material_col}` "
                f"FROM `{table_name}` "
                f"WHERE `{material_col}` IN ({placeholders})"
            )
            names: list[str] = []
            for row in conn.execute(stmt, params):
                n = row[0]
                if n:
                    names.append(str(n).strip())
        else:
            names_stmt = text(
                f"SELECT DISTINCT `{material_col}` "
                f"FROM `{table_name}` "
                f"WHERE `{material_col}` LIKE :pat"
            )
            names = []
            for row in conn.execute(names_stmt, {"pat": prefix + "%"}):
                n = row[0]
                if n:
                    names.append(str(n).strip())
    if not names:
        return []

    regions: list[RegionalPriceRegion] = []
    for name in names:
        # Latest row for this exact name
        row_stmt = text(
            f"SELECT `{date_col}` AS d, `{price_col}` AS p "
            f"FROM `{table_name}` "
            f"WHERE `{material_col}` = :n "
            f"AND `{price_col}` IS NOT NULL "
            f"ORDER BY `{date_col}` DESC LIMIT 1"
        )
        prev_stmt = text(
            f"SELECT `{price_col}` AS p "
            f"FROM `{table_name}` "
            f"WHERE `{material_col}` = :n "
            f"AND `{price_col}` IS NOT NULL "
            f"AND `{date_col}` < :last_date "
            f"ORDER BY `{date_col}` DESC LIMIT 1"
        )
        with eng.connect() as conn:
            row = conn.execute(row_stmt, {"n": name}).first()
            if not row or row[1] is None:
                continue
            try:
                last_price = float(row[1])
            except (TypeError, ValueError):
                continue
            if last_price != last_price:  # NaN
                continue
            change_pct: Optional[float] = None
            date_val = str(row[0])[:10]
            prev_row = conn.execute(prev_stmt, {
                "n": name, "last_date": date_val,
            }).first()
            if prev_row and prev_row[0] is not None:
                try:
                    prev_price = float(prev_row[0])
                    if prev_price:
                        change_pct = round(
                            (last_price - prev_price) / prev_price * 100, 2
                        )
                except (TypeError, ValueError):
                    pass
        # Supplier = part after the first '-'
        supplier = name.split("-", 1)[1] if "-" in name else name
        is_regional = any(tok in supplier for tok in _REGIONAL_TOKENS)
        is_internal = (
            any(tok in supplier for tok in _NATIONAL_AVERAGE_TOKENS)
            and not is_regional
        )
        regions.append(RegionalPriceRegion(
            price_type=supplier,
            label=supplier,
            is_internal=is_internal,
            price=round(last_price, 2),
            change_pct=change_pct,
            date=str(row[0])[:10].replace("/", "-") if row[0] else "",
            days_stale=0,
        ))
    # Regional rows first, then national/headline; alphabetical within each.
    regions.sort(key=lambda r: (
        0 if any(tok in r.price_type for tok in _REGIONAL_TOKENS) else 1,
        r.price_type,
    ))
    return regions


def get_regional_prices(product_id: str) -> RegionalPriceData:
    """Return latest price per supplier for the product.

    v2 behaviour: every distinct 厂商名supplier_name in the EDIA price table
    becomes a row, not just the regional ones. This lets the UI surface the
    headline "national average" series (布伦特 / 裂解C5均价 / 裂解C9均价 /
    日本石脑油) alongside the regional spot prices. Each row carries:
      - the latest price + date
      - change_pct = (latest - prev_day) / prev_day * 100, when both exist

    When the product has a `primary` md_t_lz_price config, the regional
    scan runs against the LIVE Longzhong feed first; the legacy long-form
    table is only consulted as a last-resort fallback.
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

    # 1. Primary source (md_t_lz_price, LIVE) — short-circuits if it has data.
    primary = cfg.get("primary")
    if primary and primary.get("table") == "md_t_lz_price":
        prefix = get_md_lz_regional_prefix(product_id)
        if prefix:
            try:
                primary_regions = _read_md_lz_regional_rows(
                    primary, prefix, eng,
                    supplier_whitelist=cfg.get("regional_suppliers"),
                )
            except Exception as exc:
                logger.warning(
                    "md_t_lz_price regional scan failed for %s: %s",
                    product_id, exc,
                )
                primary_regions = []
            if primary_regions:
                return RegionalPriceData(
                    product_id=product_id,
                    regions=primary_regions,
                    note="v2: per-supplier latest from md_t_lz_price (LIVE)",
                )
            logger.info(
                "md_t_lz_price returned 0 regional rows for %s; falling back",
                product_id,
            )
    table_name = cfg["table"]
    date_col, price_col, supplier_col, _ = _schema_for(product_id)

    # When the table has no supplier column (None) or the column is
    # overridden to a non-default schema, skip the DISTINCT supplier scan
    # and fall back to a single aggregated "Latest Order Book" row.
    if not supplier_col:
        regions: list[RegionalPriceRegion] = []
        try:
            row_stmt = text(
                f"SELECT `{date_col}` AS d, `{price_col}` AS p "
                f"FROM `{table_name}` "
                f"WHERE `{price_col}` IS NOT NULL "
                f"ORDER BY `{date_col}` DESC LIMIT 1"
            )
            prev_stmt = text(
                f"SELECT `{price_col}` AS p "
                f"FROM `{table_name}` "
                f"WHERE `{price_col}` IS NOT NULL "
                f"AND `{date_col}` < :last_date "
                f"ORDER BY `{date_col}` DESC LIMIT 1"
            )
            with eng.connect() as conn:
                row = conn.execute(row_stmt).first()
                change_pct: Optional[float] = None
                if row and row[1] is not None:
                    date_val = str(row[0])[:10]
                    prev_row = conn.execute(prev_stmt, {"last_date": date_val}).first()
                    if prev_row and prev_row[0] is not None:
                        prev_price = float(prev_row[0])
                        last_price = float(row[1])
                        if prev_price:
                            change_pct = round(
                                (last_price - prev_price) / prev_price * 100, 2
                            )
        except Exception as exc:
            logger.warning("EDIA regional query failed for %s: %s", product_id, exc)
            return RegionalPriceData(
                product_id=product_id,
                regions=[],
                note=f"aggregation query failed: {exc}",
            )
        if not row or row[1] is None:
            return RegionalPriceData(
                product_id=product_id,
                regions=[],
                note="no rows in EDIA table for this product",
            )
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            return RegionalPriceData(
                product_id=product_id,
                regions=[],
                note=f"price column could not be coerced to float",
            )
        display = "Latest Order Book"
        regions.append(RegionalPriceRegion(
            price_type=display,
            label=display,
            is_internal=True,
            price=round(price, 2),
            change_pct=change_pct,
            date=str(row[0])[:10].replace("/", "-") if row[0] else "",
            days_stale=0,
        ))
        return RegionalPriceData(
            product_id=product_id,
            regions=regions,
            note="v2: aggregated fallback (no supplier column in table)",
        )

    distinct_stmt = text(
        f"SELECT DISTINCT `{supplier_col}` FROM `{table_name}` "
        f"WHERE `{supplier_col}` IS NOT NULL"
    )
    suppliers: list[str] = []
    try:
        with eng.connect() as conn:
            for row in conn.execute(distinct_stmt):
                name = row[0]
                if name and str(name).strip():
                    suppliers.append(str(name).strip())
    except Exception as exc:
        logger.warning("EDIA regional distinct failed for %s: %s", product_id, exc)
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note=f"distinct query failed: {exc}",
        )

    # If the supplier_col override points to a non-supplier column (e.g.
    # CUST_NAME in the ERP sales tables) and the query returned something,
    # fall back to a single aggregated row.
    if supplier_col != SUPPLIER_COL and not suppliers:
        suppliers = ["__all__"]


    # If the table hosts multiple products (e.g. 裂解C5 contains Brent,
    # Naphtha and Cracked C5 as different supplier_name values), restrict
    # the candidate list to the supplier_names configured for THIS product.
    # The "__all__" sentinel bypasses the filter (used by ERP-aggregated
    # products and any table without a supplier column).
    if suppliers != ["__all__"]:
        configured = cfg.get("regional_suppliers")
        if configured:
            configured_set = {str(s).strip() for s in configured if str(s).strip()}
            suppliers = [s for s in suppliers if s in configured_set]

    if not suppliers:
        return RegionalPriceData(
            product_id=product_id,
            regions=[],
            note="no suppliers detected for this product in EDIA table",
        )

    regions: list[RegionalPriceRegion] = []
    for supplier in suppliers:
        try:
            if supplier == "__all__":
                row_stmt = text(
                    f"SELECT `{date_col}` AS d, `{price_col}` AS p "
                    f"FROM `{table_name}` "
                    f"WHERE `{price_col}` IS NOT NULL "
                    f"ORDER BY `{date_col}` DESC LIMIT 1"
                )
                prev_stmt = text(
                    f"SELECT `{price_col}` AS p "
                    f"FROM `{table_name}` "
                    f"WHERE `{price_col}` IS NOT NULL "
                    f"AND `{date_col}` < :last_date "
                    f"ORDER BY `{date_col}` DESC LIMIT 1"
                )
                stmt_params: dict = {}
            else:
                row_stmt = text(
                    f"SELECT `{date_col}` AS d, `{price_col}` AS p "
                    f"FROM `{table_name}` "
                    f"WHERE `{supplier_col}` = :supplier "
                    f"AND `{price_col}` IS NOT NULL "
                    f"ORDER BY `{date_col}` DESC LIMIT 1"
                )
                prev_stmt = text(
                    f"SELECT `{price_col}` AS p "
                    f"FROM `{table_name}` "
                    f"WHERE `{supplier_col}` = :supplier "
                    f"AND `{price_col}` IS NOT NULL "
                    f"AND `{date_col}` < :last_date "
                    f"ORDER BY `{date_col}` DESC LIMIT 1"
                )
                stmt_params = {"supplier": supplier}
            with eng.connect() as conn:
                row = conn.execute(row_stmt, stmt_params).first()
                change_pct: Optional[float] = None
                if row and row[1] is not None:
                    date_val = str(row[0])[:10]
                    prev_row = conn.execute(prev_stmt, {
                        **stmt_params,
                        "last_date": date_val,
                    }).first()
                    if prev_row and prev_row[0] is not None:
                        prev_price = float(prev_row[0])
                        last_price = float(row[1])
                        if prev_price:
                            change_pct = round(
                                (last_price - prev_price) / prev_price * 100, 2
                            )
        except Exception as exc:
            logger.warning(
                "EDIA regional query failed for %s.%s: %s",
                product_id, supplier, exc,
            )
            continue
        if not row or row[1] is None:
            continue
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        if supplier == "__all__":
            display = "Latest Order Book"
            is_regional = False
            is_internal = True
        else:
            display = supplier
            is_regional = any(tok in supplier for tok in _REGIONAL_TOKENS)
            is_internal = any(tok in supplier for tok in _NATIONAL_AVERAGE_TOKENS) and not is_regional
        regions.append(RegionalPriceRegion(
            price_type=display,
            label=display,
            is_internal=is_internal,
            price=round(price, 2),
            change_pct=change_pct,
            date=str(row[0])[:10].replace("/", "-") if row[0] else "",
            days_stale=0,
        ))
    # Regional rows first, then national/headline; alphabetical within each
    # group so columns don't shuffle.
    regions.sort(key=lambda r: (
        0 if any(tok in r.price_type for tok in _REGIONAL_TOKENS) else 1,
        r.price_type,
    ))
    return RegionalPriceData(
        product_id=product_id,
        regions=regions,
        note="v2: per-supplier latest price with prev-day change",
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
    """Read forecast prices from v_forecast_price view.

    v1 simplification: the warehouse's v_forecast_price is an audit
    backfill (max forecast_date 2025-06-12) — it does not contain live
    EDIA ML predictions. We return the price at the latest target date
    for each product, ignoring the `day` parameter. v2 can wire in
    EDIA's live ML forecast pipeline.

    Returns null for products not in MATERIAL_FORECAST_IDS or with
    no forecast data.
    """
    _require_edia()
    eng = get_mysql_engine()
    out: dict = {pid: None for pid in product_ids}
    if eng is None:
        return ForecastBaseBatchResponse(day=day, base_by_product=out)
    for pid in product_ids:
        material_ids = MATERIAL_FORECAST_IDS.get(pid, [])
        if not material_ids:
            continue
        try:
            placeholders = ",".join([f":mid_{i}" for i in range(len(material_ids))])
            params = {f"mid_{i}": m for i, m in enumerate(material_ids)}
            with eng.connect() as conn:
                row = conn.execute(text(
                    f"SELECT price FROM v_forecast_price "
                    f"WHERE material_id IN ({placeholders}) "
                    f"ORDER BY date DESC LIMIT 1"
                ), params).first()
        except Exception as exc:
            logger.warning("EDIA forecast query failed for %s: %s", pid, exc)
            continue
        if row and row[0] is not None:
            out[pid] = float(row[0])
    return ForecastBaseBatchResponse(day=day, base_by_product=out)


# ── v2: read from local ForecastRun (nightly cron output) ─────────────
# Maps dashboard product_id → ForecastTarget.product_key (seeded by
# seed_ecisco_targets). Used by get_forecast_base_batch_v2.
PRODUCT_FORECAST_TARGET_KEY: dict[str, str] = {
    "crude_oil":      "ecisco.crude_oil",
    "naphtha":        "ecisco.naphtha",
    "cracked_c5":     "ecisco.cracked_c5",
    "cracked_c9":     "ecisco.cracked_c9",
    "mixed_c5":       "ecisco.mixed_c5",
    "isoprene":       "ecisco.isoprene",
    "piperylene":     "ecisco.piperylene",
    "dcpd":           "ecisco.dcpd",
    "raffinate_c5":   "ecisco.raffinate_c5",
    "sis":            "ecisco.sis",
    "c5_resin":       "ecisco.c5_resin",
    "blowing_agent":  "ecisco.blowing_agent",
    "styrene":        "ecisco.styrene",
}

# ERP product_ids whose forecasts are keyed by material_code (SKU-level).
# The forecast target product_key is "ecisco.<product_id>.<material_code>".
_ERP_PRODUCT_KEYS: set[str] = {"c5_resin", "raffinate_c5"}


def get_forecast_base_batch_v2(
    product_ids: list[str], day: int = 7, db=None
) -> ForecastBaseBatchResponse:
    """Read forecast base prices from the local ForecastRun table.

    Replaces the v1 reader that hit EDIA's stale v_forecast_price view.
    Returns the latest `results['{day}d']['base'][0]` per product, or
    None for products with no run yet.

    The forecast_runs table is populated by the nightly automation task
    (cron 0 2 * * *) which calls ForecastEngine.compute_all().

    Phase 6: Populates overlay_by_product with intelligence + policy metadata.
    Phase 7: Populates sku_forecasts_by_product for ERP products.
    """
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.schemas.market_dashboard import (
        DecisionMeta, DirectionalEdgeMeta, ForecastOverlayMeta,
        IntelligenceOverlayMeta, PolicyOverlayMeta, ProbabilityOverlayMeta,
        SkuForecastMeta, TrustTierMeta,
    )

    out: dict = {pid: None for pid in product_ids}
    overlay: dict = {pid: None for pid in product_ids}
    sku_forecasts: dict = {pid: [] for pid in product_ids if pid in _ERP_PRODUCT_KEYS}
    horizon_key = str(day)  # results JSON uses "3"/"7"/"30" string keys
    horizon_key_alt = f"{day}d"  # legacy: some old runs used "7d" suffix

    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        # ── Separate md_t_lz_price products (direct key) from ERP products (prefix lookup) ──
        md_lz_pids = [pid for pid in product_ids if pid in PRODUCT_FORECAST_TARGET_KEY and pid not in _ERP_PRODUCT_KEYS]
        erp_pids = [pid for pid in product_ids if pid in _ERP_PRODUCT_KEYS]

        # ── md_t_lz_price products: direct product_key lookup (existing logic) ──
        if md_lz_pids:
            pid_to_key = {
                pid: PRODUCT_FORECAST_TARGET_KEY[pid]
                for pid in md_lz_pids
            }
            keys = list(pid_to_key.values())
            targets = db.query(ForecastTarget).filter(
                ForecastTarget.product_key.in_(keys),
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).all()

            # Reverse map for lookups
            key_to_pid = {v: k for k, v in pid_to_key.items()}

            for tgt in targets:
                pid = key_to_pid.get(tgt.product_key)
                if pid is None:
                    continue
                # Latest ForecastRun for this target
                run = db.query(ForecastRun).filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                ).order_by(ForecastRun.created_date.desc()).first()
                if not run or not run.results:
                    continue
                horizon_payload = run.results.get(horizon_key) or run.results.get(horizon_key_alt) or {}
                base = horizon_payload.get("base")
                if base is None:
                    continue
                # `base` is a list (from pd.Series.tolist()); take first point
                if isinstance(base, list) and len(base) > 0:
                    out[pid] = float(base[0])
                elif isinstance(base, (int, float)):
                    out[pid] = float(base)

                # Phase 6: extract overlay metadata from the run's
                # separate explanation column (NOT from horizon_payload —
                # that only carries base/bull/bear).
                explanation = (run.explanation if hasattr(run, "explanation") else None) or {}
                intel_raw = explanation.get("intelligence") or {}
                policy_raw = explanation.get("policy") or {}

                intel_meta = None
                if intel_raw:
                    intel_meta = IntelligenceOverlayMeta(
                        active_event_count=int(intel_raw.get("active_event_count", 0)),
                        bias_direction=str(intel_raw.get("bias_direction", "neutral")),
                        impact_tier=intel_raw.get("impact_tier"),
                        confidence=intel_raw.get("confidence"),
                        summary=intel_raw.get("summary"),
                    )

                policy_meta = None
                if policy_raw:
                    policy_meta = PolicyOverlayMeta(
                        volatility_regime=policy_raw.get("volatility_regime"),
                        vol_multiplier=policy_raw.get("vol_multiplier"),
                        bias_pct=policy_raw.get("bias_pct"),
                        diagnosis_bias=policy_raw.get("diagnosis_bias"),
                        applied_factor=policy_raw.get("applied_factor"),
                    )

                trust_raw = explanation.get("trust_tier") or {}
                trust_meta = None
                if trust_raw:
                    trust_meta = TrustTierMeta(
                        tier=trust_raw.get("tier", "medium"),
                        badge_color=trust_raw.get("badge_color", "yellow"),
                        badge_label_zh=trust_raw.get("badge_label_zh", ""),
                        badge_label_en=trust_raw.get("badge_label_en", ""),
                        reason_zh=trust_raw.get("reason_zh"),
                        reason_en=trust_raw.get("reason_en"),
                    )

                # Phase D: probability overlay
                probability_list: list = []
                prob_report = explanation.get("probability") or {}
                for h_str, p_info in prob_report.items():
                    if not isinstance(p_info, dict):
                        continue
                    try:
                        h_int = int(h_str)
                    except (ValueError, TypeError):
                        continue
                    pr = p_info.get("p_rise")
                    if pr is None:
                        continue
                    pr_gt_raw = p_info.get("p_rise_gt") or {}
                    pr_gt = {str(k): float(v) for k, v in pr_gt_raw.items() if v is not None}
                    probability_list.append(ProbabilityOverlayMeta(
                        horizon=h_int,
                        p_rise=float(pr),
                        p_rise_gt=pr_gt,
                        expected_change_pct=p_info.get("expected_change_pct"),
                    ))

                # Phase E1: directional edge list
                directional_list: list = []
                dir_report = explanation.get("directional") or {}
                for h_str, d_info in dir_report.items():
                    if not isinstance(d_info, dict):
                        continue
                    try:
                        h_int = int(h_str)
                    except (ValueError, TypeError):
                        continue
                    acc = d_info.get("accuracy")
                    if acc is None:
                        continue
                    directional_list.append(DirectionalEdgeMeta(
                        horizon=h_int,
                        accuracy=float(acc),
                        n_test=int(d_info.get("n_test", 0) or 0),
                        p_value=float(d_info.get("p_value", 1.0) or 1.0),
                        status=str(d_info.get("status", "no_edge")),
                    ))

                # Phase E2: decision list
                decision_list: list = []
                dec_report = explanation.get("decision") or {}
                for h_str, dec_info in dec_report.items():
                    if not isinstance(dec_info, dict):
                        continue
                    try:
                        h_int = int(h_str)
                    except (ValueError, TypeError):
                        continue
                    decision_list.append(DecisionMeta(
                        horizon=h_int,
                        action=str(dec_info.get("action", "watch")),
                        confidence=str(dec_info.get("confidence", "low")),
                        rationale=str(dec_info.get("rationale", "")),
                    ))

                if (
                    intel_meta or policy_meta or trust_meta
                    or probability_list or directional_list or decision_list
                ):
                    overlay[pid] = ForecastOverlayMeta(
                        product_id=pid,
                        intelligence=intel_meta,
                        policy=policy_meta,
                        trust_tier=trust_meta,
                        probability=probability_list if probability_list else None,
                        directional=directional_list if directional_list else None,
                        decision=decision_list if decision_list else None,
                    )

        # ── ERP products: prefix lookup for SKU-level targets ──
        for pid in erp_pids:
            prefix = f"ecisco.{pid}."
            sku_targets = db.query(ForecastTarget).filter(
                ForecastTarget.product_key.like(f"{prefix}%"),
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).order_by(ForecastTarget.report_order.asc()).all()

            if not sku_targets:
                continue

            sku_list: list[SkuForecastMeta] = []
            for tgt in sku_targets:
                # Extract material_code from product_key: "ecisco.c5_resin.CODE" → "CODE"
                material_code = tgt.product_key[len(prefix):]

                # Latest ForecastRun for this SKU target
                run = db.query(ForecastRun).filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                ).order_by(ForecastRun.created_date.desc()).first()

                sku_base = None
                sku_confidence = None
                if run and run.results:
                    horizon_payload = run.results.get(horizon_key) or run.results.get(horizon_key_alt) or {}
                    base = horizon_payload.get("base")
                    if isinstance(base, list) and len(base) > 0:
                        sku_base = float(base[0])
                    elif isinstance(base, (int, float)):
                        sku_base = float(base)
                    sku_confidence = run.confidence

                sku_meta = SkuForecastMeta(
                    material_code=material_code,
                    target_id=str(tgt.id),
                    name=tgt.name,
                    report_order=tgt.report_order or 99,
                    is_primary=(tgt.report_order is not None and tgt.report_order <= 12),
                    forecast_base=sku_base,
                    forecast_confidence=sku_confidence,
                )
                sku_list.append(sku_meta)

                # Primary SKU (first in list, lowest report_order) feeds the main forecast
                if sku_meta.is_primary and sku_base is not None and out[pid] is None:
                    out[pid] = sku_base

            sku_forecasts[pid] = sku_list

    finally:
        if owns_session:
            db.close()

    return ForecastBaseBatchResponse(
        day=day,
        base_by_product=out,
        overlay_by_product=overlay,
        sku_forecasts_by_product=sku_forecasts,
    )


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
               raw_quote, relevance_to_c5_c9, key_information, detected_at,
               time_horizon, geographic_scope, key_entities
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
            summary=r[15],
            detected_at=str(r[16] or ""),
            time_horizon=r[17],
            geographic_scope=r[18],
            key_entities=_safe_json_list(r[19]),
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


# ── Overview one-shot bundle ────────────────────────────────────────────

from app.schemas.market_dashboard import (
    RegionalBatchResponse, ForecastBaseBatchResponse,
    OverviewBundle,
)  # noqa: E402  imported here to avoid changing the existing block


def get_grade_kpis_batch(product_ids: list[str]) -> dict[str, GradeKpiData]:
    """Query grade-level (牌号 / material_model) supply-chain KPIs.

    Data sources (read-only EDIA MySQL):
      * sales 30d — ``erp_v_sale_orderentry.FDELIQTY`` delivered in the last 30d
      * pending   — ``erp_v_sale_orderentry.FREMAINOUTQTY`` (remaining to ship)
      * inventory — ``erp_v_stk_inventory.FBASEQTY``

    Rows are grouped by ``material_code`` (SKU). The 牌号 label comes from
    ``material_model``; for SKUs that only appear in inventory (no recent
    sales) the ``material`` master table supplies the label.

    Upstream products (crude_oil, naphtha, mixed_c5) have no ERP mapping, so
    they naturally yield empty results.
    """
    out: dict[str, GradeKpiData] = {
        pid: GradeKpiData(product_id=pid) for pid in product_ids
    }

    eng = get_mysql_engine()
    if eng is None:
        return out

    # One flat name list; each material_name maps to exactly one product.
    name_to_pid: dict[str, str] = {}
    for pid in product_ids:
        for name in _PRODUCT_MATERIAL_NAMES.get(pid, []):
            name_to_pid.setdefault(name, pid)

    if not name_to_pid:
        return out

    names = list(name_to_pid.keys())
    name_ph, name_params = _material_name_params(names)

    # Reference date = latest delivery date in the warehouse; consistent with
    # ErpKpiService. Falls back to today on any error.
    ref_date = date.today()
    try:
        with eng.connect() as conn:
            ref_row = conn.execute(text(
                "SELECT MAX(DATE(PLANDATE)) FROM erp_v_sale_orderentry "
                "WHERE PLANDATE IS NOT NULL"
            )).first()
            if ref_row and ref_row[0] is not None:
                ref_date = _parse_date(ref_row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade-kpis ref-date query failed: %s", exc)

    ref_str = ref_date.isoformat()
    cutoff = (ref_date - timedelta(days=30)).isoformat()

    # Expose the actual 30-day window so callers can render "as of" dates.
    for data in out.values():
        data.window_start = cutoff
        data.window_end = ref_str

    # ── sales + pending per SKU ──────────────────────────────────────────
    sales_map: dict[str, dict] = {}
    try:
        with eng.connect() as conn:
            sp = dict(name_params)
            sp["start_date"] = cutoff
            sp["end_date"] = ref_str
            rows = conn.execute(text(
                f"SELECT material_code, material_name, material_model, "
                f"  SUM(CASE WHEN PLANDATE IS NOT NULL "
                f"         AND DATE(PLANDATE) >= :start_date "
                f"         AND DATE(PLANDATE) <= :end_date "
                f"      THEN COALESCE(FDELIQTY, 0) ELSE 0 END) AS sales_30d, "
                f"  SUM(CASE WHEN COALESCE(FREMAINOUTQTY, 0) > 0 "
                f"      THEN FREMAINOUTQTY ELSE 0 END) AS pending "
                f"FROM erp_v_sale_orderentry "
                f"WHERE material_name IN ({name_ph}) "
                f"  AND material_code IS NOT NULL AND material_code <> '' "
                f"GROUP BY material_code, material_name, material_model"
            ), sp).all()
        for r in rows:
            code = str(r[0] or "").strip()
            if not code:
                continue
            model = str(r[2] or "").strip()
            entry = sales_map.setdefault(code, {
                "name": str(r[1] or "").strip(),
                "model": model,
                "sales_30d": 0.0,
                "pending": 0.0,
            })
            if model and not entry["model"]:
                entry["model"] = model
            entry["sales_30d"] += float(r[3] or 0.0)
            entry["pending"] += float(r[4] or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade-kpis sales query failed: %s", exc)

    # ── inventory per SKU ────────────────────────────────────────────────
    inv_map: dict[str, float] = {}
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                f"SELECT material_code, SUM(COALESCE(FBASEQTY, 0)) AS inv_tons "
                f"FROM erp_v_stk_inventory "
                f"WHERE material_name IN ({name_ph}) "
                f"  AND material_code IS NOT NULL AND material_code <> '' "
                f"GROUP BY material_code"
            ), dict(name_params)).all()
        for r in rows:
            code = str(r[0] or "").strip()
            if not code:
                continue
            inv_map[code] = float(r[1] or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade-kpis inventory query failed: %s", exc)

    # ── canonical 牌号 label for inventory-only SKUs ─────────────────────
    master_model: dict[str, str] = {}
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                f"SELECT material_code, MAX(material_model) AS model "
                f"FROM material "
                f"WHERE material_name IN ({name_ph}) "
                f"  AND material_code IS NOT NULL AND material_code <> '' "
                f"GROUP BY material_code"
            ), dict(name_params)).all()
        for r in rows:
            if r[0] and r[1]:
                master_model[str(r[0]).strip()] = str(r[1]).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade-kpis material-master query failed: %s", exc)

    # ── merge + bucket by product ────────────────────────────────────────
    all_codes: set[str] = set(sales_map) | set(inv_map)
    for code in all_codes:
        s = sales_map.get(code) or {}
        name = s.get("name") or ""
        pid = name_to_pid.get(name)
        if pid is None:
            continue

        model = (s.get("model") or master_model.get(code) or "").strip()
        grade = model if model else code

        sales_30d = float(s.get("sales_30d") or 0.0)
        pending = float(s.get("pending") or 0.0)
        inv_tons = inv_map.get(code)

        inventory_days = None
        if inv_tons is not None and sales_30d > 0:
            inventory_days = round(inv_tons / (sales_30d / 30.0), 2)

        out[pid].rows.append(GradeKpiRow(
            grade=grade,
            sku_code=code,
            sales_30d_tons=round(sales_30d, 2) if sales_30d else None,
            inventory_tons=round(inv_tons, 2) if inv_tons is not None else None,
            inventory_days=inventory_days,
            pending_tons=round(pending, 2) if pending else None,
        ))

    for pid in out:
        out[pid].rows.sort(
            key=lambda r: (r.sales_30d_tons is None, -(r.sales_30d_tons or 0.0), r.sku_code)
        )
        out[pid].is_live = bool(out[pid].rows)

    return out


def get_supply_chain(product_id: str) -> GradeKpiData:
    """Return per-product supply-chain snapshot (grade KPIs + summary).

    Thin wrapper over ``get_grade_kpis_batch`` — reuses the single SQL
    implementation and only adds the presentation-only ``summary`` aggregate
    (totals + weighted ``avg_days_pending``). Does NOT modify the batch
    function, which is shared by OverviewBundle / price-chain / ECISCO BI.

    ``avg_days_pending`` = Σ(days_pendingᵢ · pending_tonsᵢ) / Σ(pending_tonsᵢ)
    over rows where pending_tons > 0 and sales_30d_tons > 0, with
    ``days_pendingᵢ = (pending_tonsᵢ / sales_30d_tonsᵢ) × 30``.
    """
    data = get_grade_kpis_batch([product_id])[product_id]
    rows = data.rows

    sales_30d_tons = sum(r.sales_30d_tons for r in rows if r.sales_30d_tons)
    inventory_tons = sum(r.inventory_tons for r in rows if r.inventory_tons)
    pending_tons = sum(r.pending_tons for r in rows if r.pending_tons)

    weighted_num = 0.0
    weighted_den = 0.0
    for r in rows:
        p = r.pending_tons
        s = r.sales_30d_tons
        if p and p > 0 and s and s > 0:
            days_pending = (p / s) * 30.0
            weighted_num += days_pending * p
            weighted_den += p
    avg_days_pending = round(weighted_num / weighted_den, 2) if weighted_den > 0 else None

    data.summary = GradeKpiSummary(
        sales_30d_tons=round(sales_30d_tons, 2) if sales_30d_tons else None,
        inventory_tons=round(inventory_tons, 2) if inventory_tons else None,
        pending_tons=round(pending_tons, 2) if pending_tons else None,
        avg_days_pending=avg_days_pending,
    )
    return data


def get_overview_bundle(
    bypass_cache: bool = False, forecast_day: int = 7,
) -> OverviewBundle:
    """Return one snapshot + regional + forecast + fx payload in a single call.

    Uses the in-memory 30s cache so a fleet of users browsing the dashboard
    doesn't each hit the WAN to MySQL. `?no_cache=1` (bypass_cache=True)
    forces a fresh build — used by the Refresh button.
    """
    cache_key = f"overview:bundle:f{forecast_day}"
    if not bypass_cache:
        cached, age_ms = _cache_get(cache_key)
        if cached is not None:
            cached["cached"] = True
            cached["cache_age_ms"] = age_ms
            return OverviewBundle(**cached)

    pids = _overview_product_ids()

    # 1. Snapshot for all 12 products.
    snap = get_market_snapshot(group=None)

    # 2. Regional batch — one round-trip to MySQL hits every table.
    regional_resp = get_regional_prices_batch(pids)
    regional = regional_resp.items
    regional_errors = regional_resp.errors

    # 3. Forecast 7-day base per product + overlay (v2, Phase 6).
    forecast_resp = get_forecast_base_batch_v2(pids, day=forecast_day)

    # 4. Grade-level supply-chain KPIs (ERP products only).
    grade_kpis = get_grade_kpis_batch(pids)

    # 5. FX (static for v1).
    fx = get_fx_rate()

    bundle = OverviewBundle(
        snapshot=snap,
        regional=regional,
        regional_errors=regional_errors,
        forecast_day=forecast_day,
        forecast_base_by_product=forecast_resp.base_by_product,
        forecast_overlay_by_product=forecast_resp.overlay_by_product,  # Phase 6
        grade_kpis_by_product=grade_kpis,
        fx=fx,
        cached=False,
        cache_age_ms=0,
    )
    # Store a plain dict representation so future cache hits can copy without
    # rebuilding Pydantic models.
    payload = bundle.model_dump()
    _cache_put(cache_key, payload)
    return bundle


def invalidate_overview_cache() -> int:
    """Drop overview cache entries. Returns count dropped."""
    return _cache_invalidate_prefix("overview:bundle:")


# ── Forecast chart: merged past + forecast timeline ───────────────────

def _build_overlay_meta(
    product_id: str, horizon_payload: dict,
    explanation: Optional[dict] = None,
) -> Optional["ForecastOverlayMeta"]:
    """Phase 6: Build overlay metadata from a ForecastRun horizon_payload.

    *explanation* should be the dict from the ForecastRun's separate
    ``explanation`` JSON column (NOT from ``horizon_payload`` — that only
    contains ``{base, bull, bear}``).  Falls back to
    ``horizon_payload.get("explanation")`` for backward compat.
    """
    from app.schemas.market_dashboard import (
        DecisionMeta, DirectionalEdgeMeta, ForecastOverlayMeta,
        IntelligenceOverlayMeta, PolicyOverlayMeta, ProbabilityOverlayMeta,
        TrustTierMeta,
    )
    if explanation is None:
        explanation = horizon_payload.get("explanation") or {}
    intel_raw = explanation.get("intelligence") or {}
    policy_raw = explanation.get("policy") or {}

    intel_meta: Optional[IntelligenceOverlayMeta] = None
    if intel_raw:
        intel_meta = IntelligenceOverlayMeta(
            active_event_count=int(intel_raw.get("active_event_count", 0)),
            bias_direction=str(intel_raw.get("bias_direction", "neutral")),
            impact_tier=intel_raw.get("impact_tier"),
            confidence=intel_raw.get("confidence"),
            summary=intel_raw.get("summary"),
        )

    policy_meta: Optional[PolicyOverlayMeta] = None
    if policy_raw:
        policy_meta = PolicyOverlayMeta(
            volatility_regime=policy_raw.get("volatility_regime"),
            vol_multiplier=policy_raw.get("vol_multiplier"),
            bias_pct=policy_raw.get("bias_pct"),
            diagnosis_bias=policy_raw.get("diagnosis_bias"),
            applied_factor=policy_raw.get("applied_factor"),
        )

    trust_raw = explanation.get("trust_tier") or {}
    trust_meta: Optional[TrustTierMeta] = None
    if trust_raw:
        trust_meta = TrustTierMeta(
            tier=trust_raw.get("tier", "medium"),
            badge_color=trust_raw.get("badge_color", "yellow"),
            badge_label_zh=trust_raw.get("badge_label_zh", ""),
            badge_label_en=trust_raw.get("badge_label_en", ""),
            reason_zh=trust_raw.get("reason_zh"),
            reason_en=trust_raw.get("reason_en"),
        )

    # Phase D: probability overlay (price-change probability per horizon)
    probability_list: list[ProbabilityOverlayMeta] = []
    prob_report = explanation.get("probability") or {}
    for h_str, p_info in prob_report.items():
        if not isinstance(p_info, dict):
            continue
        try:
            h_int = int(h_str)
        except (ValueError, TypeError):
            continue
        p_rise = p_info.get("p_rise")
        if p_rise is None:
            continue
        p_rise_gt_raw = p_info.get("p_rise_gt") or {}
        p_rise_gt = {str(k): float(v) for k, v in p_rise_gt_raw.items() if v is not None}
        probability_list.append(ProbabilityOverlayMeta(
            horizon=h_int,
            p_rise=float(p_rise),
            p_rise_gt=p_rise_gt,
            expected_change_pct=p_info.get("expected_change_pct"),
        ))

    # Phase E1: directional edge list
    directional_list: list[DirectionalEdgeMeta] = []
    dir_report = explanation.get("directional") or {}
    for h_str, d_info in dir_report.items():
        if not isinstance(d_info, dict):
            continue
        try:
            h_int = int(h_str)
        except (ValueError, TypeError):
            continue
        acc = d_info.get("accuracy")
        if acc is None:
            continue
        directional_list.append(DirectionalEdgeMeta(
            horizon=h_int,
            accuracy=float(acc),
            n_test=int(d_info.get("n_test", 0) or 0),
            p_value=float(d_info.get("p_value", 1.0) or 1.0),
            status=str(d_info.get("status", "no_edge")),
        ))

    # Phase E2: decision list
    decision_list: list[DecisionMeta] = []
    dec_report = explanation.get("decision") or {}
    for h_str, dec_info in dec_report.items():
        if not isinstance(dec_info, dict):
            continue
        try:
            h_int = int(h_str)
        except (ValueError, TypeError):
            continue
        decision_list.append(DecisionMeta(
            horizon=h_int,
            action=str(dec_info.get("action", "watch")),
            confidence=str(dec_info.get("confidence", "low")),
            rationale=str(dec_info.get("rationale", "")),
        ))

    if (
        intel_meta or policy_meta or trust_meta
        or probability_list or directional_list or decision_list
    ):
        return ForecastOverlayMeta(
            product_id=product_id,
            intelligence=intel_meta,
            policy=policy_meta,
            trust_tier=trust_meta,
            probability=probability_list if probability_list else None,
            directional=directional_list if directional_list else None,
            decision=decision_list if decision_list else None,
        )
    return None


def _extract_horizon_keyed(explanation: Optional[dict], section: str, day: int) -> dict:
    """Pull {"7": {...}} style section out of run.explanation for horizon `day`.

    Falls back to the first entry when the exact horizon is absent.
    """
    raw = (explanation or {}).get(section) or {}
    if not isinstance(raw, dict):
        return {}
    exact = raw.get(str(day))
    if isinstance(exact, dict):
        return exact
    for v in raw.values():
        if isinstance(v, dict):
            return v
    return {}


def _extract_drivers(explanation: Optional[dict]) -> list:
    """Extract top-N feature drivers from run.explanation (Stream B2).

    The engine stores drivers as [{"feature": str, "weight": float}, ...]
    in run.explanation["drivers"]. Returns DriverAttributionMeta list.
    """
    from app.schemas.market_dashboard import DriverAttributionMeta

    if not explanation or not isinstance(explanation, dict):
        return []
    raw_drivers = explanation.get("drivers")
    if not raw_drivers or not isinstance(raw_drivers, list):
        return []
    out = []
    for d in raw_drivers[:5]:  # top-5 only
        if isinstance(d, dict) and "feature" in d and "weight" in d:
            try:
                out.append(DriverAttributionMeta(
                    feature=str(d["feature"]),
                    weight=float(d["weight"]),
                ))
            except (TypeError, ValueError):
                continue
    return out


def _build_analysis_block(
    *,
    day: int,
    history_rows: list,
    horizon_payload: dict,
    run=None,
    explanation: Optional[dict] = None,
    overlay: Optional["ForecastOverlayMeta"] = None,
) -> "ForecastAnalysisMeta":
    """Compose the AI 分析依据 block for the chart page / decision board.

    Combines read-time trend stats (from actual history) with run-level
    model/uncertainty/directional evidence, then renders the Chinese
    【预测依据】 + 【建议逻辑】 fragments via forecasting.narrative.
    """
    from app.schemas.market_dashboard import ForecastAnalysisMeta
    from app.services.forecasting import narrative as fn

    trend = fn.compute_trend_stats(history_rows)
    unc = fn.compute_uncertainty_stats(horizon_payload)
    models = fn.compute_model_stats(
        getattr(run, "model_detail", None),
        getattr(run, "below_naive_baseline", None),
    )

    dire = _extract_horizon_keyed(explanation, "directional", day)
    dec = _extract_horizon_keyed(explanation, "decision", day)
    prob = _extract_horizon_keyed(explanation, "probability", day)
    trust_raw = (explanation or {}).get("trust_tier") or {}

    # Prefer overlay values (already normalised) when available
    action = dec.get("action", "watch")
    confidence = dec.get("confidence", "low")
    p_rise = prob.get("p_rise")
    exp_chg = prob.get("expected_change_pct")
    dir_acc = dire.get("accuracy")
    dir_status = dire.get("status")
    trust_tier = trust_raw.get("tier")

    basis_zh = fn.build_basis_zh(trend=trend, uncertainty=unc, models=models)
    action_logic_zh = fn.build_action_logic_zh(
        action=action, confidence=confidence,
        p_rise=p_rise, expected_change_pct=exp_chg,
        directional_accuracy=dir_acc, directional_status=dir_status,
        trust_tier=trust_tier,
    )

    return ForecastAnalysisMeta(
        chg_7d_pct=trend.get("chg_7d_pct"),
        chg_30d_pct=trend.get("chg_30d_pct"),
        ma30=trend.get("ma30"),
        above_ma30=trend.get("above_ma30"),
        bull_price=unc.get("bull"),
        bear_price=unc.get("bear"),
        spread_pct=unc.get("spread_pct"),
        model_count=models.get("model_count", 0),
        model_names=models.get("model_names", []),
        ensemble_mape=models.get("ensemble_mape"),
        naive_mape=models.get("naive_mape"),
        beats_naive=models.get("beats_naive"),
        below_naive_baseline=models.get("below_naive_baseline"),
        directional_accuracy=float(dir_acc) if isinstance(dir_acc, (int, float)) else None,
        directional_status=dir_status,
        directional_n_test=int(dire.get("n_test", 0) or 0) or None,
        rationale=dec.get("rationale"),
        drivers=_extract_drivers(explanation),
        basis_zh=basis_zh,
        action_logic_zh=action_logic_zh,
    )


def _pick_horizon_payload(results: dict, days: int) -> tuple:
    """Return (horizon_payload, effective_horizon_days) for the requested days.

    Preference: exact key f"{days}d" → str(days); otherwise the nearest
    available numeric horizon key (ties → larger key).  Returns ({}, None)
    if no usable horizon is found.

    Delegates to forecasting.answer_format.pick_horizon_payload — the shared
    implementation the chat agent's forecast_get/forecast_run tools also use,
    so dashboard and chat always select the same horizon slice of a run.
    """
    from app.services.forecasting.answer_format import pick_horizon_payload

    return pick_horizon_payload(results, days)


def _slice_curve(curve, days: int) -> list[float] | None:
    """Slice a curve array to at most `days` points.  Returns None if curve
    is not a list or is empty."""
    if not isinstance(curve, list) or len(curve) == 0:
        return None
    return curve[:days]


def _curve_to_float_list(curve) -> list[float] | None:
    """Normalise a curve value (list / scalar / None) into a list of floats."""
    if isinstance(curve, list) and len(curve) > 0:
        return [float(v) for v in curve]
    elif isinstance(curve, (int, float)):
        return [float(curve)]
    return None


def get_forecast_chart(
    product_id: str, days: int = 30, db=None,
) -> "ForecastChartResponse":
    """Return merged past-actual + step-function previous-AI + forecast timeline.

    EDIA overlay-chart semantics:
      - Left zone (past): actual price (royal-blue), step-function previous-AI
        dashed line, subtle past-range band (prev_ai_low / prev_ai_high),
        and executed-in/out-of-range scatter dots.
      - Right zone (forecast): bull / base / bear dotted curves fanning from
        today.  No green band — the trio of dotted lines IS the range.
      - Boundary bridging: today point seeded with base=bull=bear=actual so
        lines connect smoothly.

    Phase 6: Includes overlay metadata (intelligence + policy) from the
    latest ForecastRun's explanation.
    """
    from app.schemas.market_dashboard import (
        ForecastChartPoint, ForecastChartResponse,
        ForecastOverlayMeta, IntelligenceOverlayMeta, PolicyOverlayMeta,
    )
    from app.models.forecasting import ForecastTarget, ForecastRun
    from datetime import date as dt_date

    label = PRODUCT_LABELS.get(product_id, {"en": product_id, "zh": product_id})

    # 1. Past price history
    history_rows = _read_product_history_rows(product_id)

    # 2. Forecast runs — fetch ALL (not just 2) for step-function AI review
    target_key = PRODUCT_FORECAST_TARGET_KEY.get(product_id)
    forecast_curves: dict[str, list[float]] = {}
    previous_ai_series: dict[str, Optional[float]] = {}
    prev_ai_low_series: dict[str, Optional[float]] = {}
    prev_ai_high_series: dict[str, Optional[float]] = {}
    exec_in_by_date: dict[str, float] = {}
    exec_out_by_date: dict[str, float] = {}
    data_quality = "insufficient"
    as_of_date: Optional[str] = None
    previous_ai_date: Optional[str] = None
    executed_in_count = 0
    executed_out_count = 0
    overlay_meta: Optional[ForecastOverlayMeta] = None  # Phase 6
    analysis_meta = None  # ForecastAnalysisMeta | None — AI 分析依据

    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        if target_key:
            tgt = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == target_key,
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).first()

            if tgt:
                runs = db.query(ForecastRun).filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                ).order_by(ForecastRun.created_date.desc()).limit(10).all()

                if runs:
                    # Horizon-aware latest-run selection: pick the newest run
                    # whose results actually contain the requested horizon.
                    latest = None
                    for r in runs:
                        rp = (r.results or {})
                        if f"{days}d" in rp or str(days) in rp:
                            latest = r
                            break
                    if latest is None:
                        latest = runs[0]  # fallback to newest (nearest-horizon slicing)

                    horizon_payload, effective_horizon = _pick_horizon_payload(
                        latest.results or {}, days,
                    )
                    for k in ("base", "bull", "bear"):
                        raw = horizon_payload.get(k)
                        curve = _curve_to_float_list(raw)
                        if curve is not None and effective_horizon is not None and effective_horizon > days:
                            curve = curve[:days]
                        if curve is not None:
                            forecast_curves[k] = curve
                    as_of_date = (
                        latest.created_date.strftime("%Y-%m-%d")
                        if latest.created_date else None
                    )
                    data_quality = "good"

                    # ---- Phase 6: Extract overlay metadata ----
                    # explanation is a separate column on ForecastRun — NOT
                    # embedded in horizon_payload (which only has base/bull/bear).
                    overlay_meta = _build_overlay_meta(
                        product_id, horizon_payload, latest.explanation,
                    )

                    # ---- AI 分析依据 (chart-page reasoning panel) ----
                    analysis_meta = _build_analysis_block(
                        day=days,
                        history_rows=history_rows,
                        horizon_payload=horizon_payload,
                        run=latest,
                        explanation=latest.explanation,
                        overlay=overlay_meta,
                    )

                    # ---- Step-function previous AI across the PAST zone ----
                    # For each past date D, find the newest run with
                    # created_date <= D, then lookup curves[offset] where
                    # offset = days between run_date and D.
                    #
                    # Runs sorted oldest-first for the scan.
                    runs_sorted = sorted(runs, key=lambda r: r.created_date)

                    if history_rows:
                        # Use only the last `days` entries for the past zone
                        recent_history = history_rows[-days:] if len(history_rows) >= days else history_rows

                        for d_str, _actual_val in recent_history:
                            try:
                                d_date = dt_date.fromisoformat(d_str)
                            except (ValueError, TypeError):
                                continue

                            # Find newest run <= this date
                            best_run = None
                            for r in runs_sorted:
                                rd = r.created_date.date() if hasattr(r.created_date, "date") else r.created_date
                                if rd <= d_date:
                                    best_run = r
                                else:
                                    break  # runs_sorted is oldest-first → rest are newer

                            if best_run is None:
                                continue

                            best_date = best_run.created_date.date() if hasattr(best_run.created_date, "date") else best_run.created_date
                            offset = (d_date - best_date).days
                            if offset < 0:
                                continue

                            # Use _pick_horizon_payload for nearest-horizon fallback
                            bp, bp_horizon = _pick_horizon_payload(
                                best_run.results or {}, days,
                            )
                            base_arr = bp.get("base")
                            bull_arr = bp.get("bull")
                            bear_arr = bp.get("bear")

                            # Slice if the fallback horizon is larger than requested
                            if bp_horizon is not None and bp_horizon > days:
                                if isinstance(base_arr, list) and 0 <= offset < min(bp_horizon, len(base_arr)):
                                    if offset < days:
                                        previous_ai_series[d_str] = float(base_arr[offset])
                                if isinstance(bull_arr, list) and 0 <= offset < min(bp_horizon, len(bull_arr)):
                                    if offset < days:
                                        prev_ai_high_series[d_str] = float(bull_arr[offset])
                                if isinstance(bear_arr, list) and 0 <= offset < min(bp_horizon, len(bear_arr)):
                                    if offset < days:
                                        prev_ai_low_series[d_str] = float(bear_arr[offset])
                            else:
                                if isinstance(base_arr, list) and 0 <= offset < len(base_arr):
                                    previous_ai_series[d_str] = float(base_arr[offset])
                                elif isinstance(base_arr, (int, float)):
                                    previous_ai_series[d_str] = float(base_arr)
                                if isinstance(bull_arr, list) and 0 <= offset < len(bull_arr):
                                    prev_ai_high_series[d_str] = float(bull_arr[offset])
                                if isinstance(bear_arr, list) and 0 <= offset < len(bear_arr):
                                    prev_ai_low_series[d_str] = float(bear_arr[offset])

                            # Executed-dot classification
                            lo = prev_ai_low_series.get(d_str)
                            hi = prev_ai_high_series.get(d_str)
                            if lo is not None and hi is not None and _actual_val is not None:
                                if lo <= _actual_val <= hi:
                                    exec_in_by_date[d_str] = _actual_val
                                else:
                                    exec_out_by_date[d_str] = _actual_val

                    # Tally counts
                    executed_in_count = len(exec_in_by_date)
                    executed_out_count = len(exec_out_by_date)

                    # Previous AI date = newest run that is NOT the latest
                    if len(runs) > 1:
                        prev_ai_run = runs[1]
                        previous_ai_date = (
                            prev_ai_run.created_date.strftime("%Y-%m-%d")
                            if prev_ai_run.created_date else None
                        )
    finally:
        if owns_session:
            db.close()

    # 3. Merge into timeline points with boundary bridging
    today = dt_date.today()
    today_str = today.isoformat()

    actual_by_date: dict[str, float] = {}
    for date_str, price in history_rows:
        actual_by_date[date_str] = price

    # Past dates from history (last `days` entries)
    all_dates_set = set()
    recent_history: list[tuple[str, float]] = []
    if history_rows:
        recent_history = history_rows[-days:] if len(history_rows) >= days else history_rows
        for d, _ in recent_history:
            all_dates_set.add(d)

    # Forecast dates: build from today forward
    forecast_len = len(forecast_curves.get("base", []))
    for i in range(forecast_len):
        fwd_date = (today + timedelta(days=i)).isoformat()
        all_dates_set.add(fwd_date)

    # Include previous AI dates
    for d in list(previous_ai_series.keys()) + list(exec_in_by_date.keys()) + list(exec_out_by_date.keys()):
        all_dates_set.add(d)

    # Always include today for boundary bridging
    all_dates_set.add(today_str)

    all_dates = sorted(all_dates_set)

    # Boundary-bridging: today point gets actual price as base/bull/bear seed
    today_actual = actual_by_date.get(today_str)

    points: list[ForecastChartPoint] = []
    for d in all_dates:
        zone = "past" if d < today_str else ("today" if d == today_str else "forecast")
        actual = actual_by_date.get(d)
        previous_ai = previous_ai_series.get(d)
        prev_low = prev_ai_low_series.get(d)
        prev_high = prev_ai_high_series.get(d)
        exec_in = exec_in_by_date.get(d)
        exec_out = exec_out_by_date.get(d)

        # Forecast curves mapped by offset from today
        base_val = None
        bull_val = None
        bear_val = None

        if zone == "today":
            # Boundary bridge: seed forecast curves from actual
            if today_actual is not None:
                base_val = today_actual
                bull_val = today_actual
                bear_val = today_actual
        elif zone == "forecast" and forecast_curves.get("base"):
            try:
                parsed = dt_date.fromisoformat(d)
                offset = (parsed - today).days
            except (ValueError, TypeError):
                offset = None
            if offset is not None and 0 <= offset < forecast_len:
                base_val = _safe_idx(forecast_curves.get("base"), offset)
                bull_val = _safe_idx(forecast_curves.get("bull"), offset)
                bear_val = _safe_idx(forecast_curves.get("bear"), offset)

        # Also add actual to today point for the big dot
        if zone == "today" and actual is None:
            actual = today_actual

        # Include any point that carries data
        has_data = (
            actual is not None
            or previous_ai is not None
            or base_val is not None
            or exec_in is not None
            or exec_out is not None
            or prev_low is not None
            or prev_high is not None
        )
        if has_data:
            points.append(ForecastChartPoint(
                date=d,
                actual=actual,
                previous_ai=previous_ai,
                prev_ai_low=prev_low,
                prev_ai_high=prev_high,
                exec_in=exec_in,
                exec_out=exec_out,
                base=base_val,
                bull=bull_val,
                bear=bear_val,
                zone=zone,
            ))

    return ForecastChartResponse(
        product_id=product_id,
        points=points,
        data_quality=data_quality,
        as_of_date=as_of_date,
        previous_ai_date=previous_ai_date,
        product_name=label.get("en", product_id),
        executed_in_range_count=executed_in_count,
        executed_outside_range_count=executed_out_count,
        overlay=overlay_meta,  # Phase 6
        analysis=analysis_meta,  # AI 分析依据
    )


def get_forecast_analysis(
    product_id: str, day: int = 7, db=None,
) -> Optional["ForecastAnalysisMeta"]:
    """Lightweight accessor for the AI 分析依据 block of one product.

    Used by the decision-board service to enrich narratives without
    paying for the full chart point assembly. Returns None when the
    product has no forecast target / run / history.
    """
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.schemas.market_dashboard import ForecastAnalysisMeta  # noqa: F401

    target_key = PRODUCT_FORECAST_TARGET_KEY.get(product_id)
    if not target_key:
        return None

    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        tgt = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == target_key,
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if tgt is None:
            return None

        run = db.query(ForecastRun).filter(
            ForecastRun.target_id == tgt.id,
            ForecastRun.is_deleted == False,  # noqa: E712
        ).order_by(ForecastRun.created_date.desc()).first()
        if run is None or not run.results:
            return None

        horizon_payload, _eh = _pick_horizon_payload(run.results, day)
        history_rows = _read_product_history_rows(product_id)
        if not history_rows and not horizon_payload:
            return None

        return _build_analysis_block(
            day=day,
            history_rows=history_rows,
            horizon_payload=horizon_payload,
            run=run,
            explanation=run.explanation,
            overlay=None,
        )
    finally:
        if owns_session:
            db.close()


def _safe_idx(lst: Optional[list], idx: int) -> Optional[float]:
    """Safely get a float at an index from a list, returning None on any error."""
    if lst is None:
        return None
    try:
        return float(lst[idx])
    except (IndexError, TypeError, ValueError):
        return None


# ── Price transmission chain (3-tier upstream flow) ────────────────────

# Hardcoded 3-tier chain: crude → naphtha → cracked C5/C9 → derivatives
_PRICE_CHAIN_TIERS: list[list[str]] = [
    ["crude_oil"],
    ["naphtha"],
    ["cracked_c5", "cracked_c9"],
    ["isoprene", "piperylene", "dcpd"],
]


def get_price_chain(db=None) -> "PriceChainResponse":
    """Return 3-tier upstream price transmission chain.

    Each node includes: current price, 30d change, sparkline (last 30
    daily prices), percentile vs ~3yr data, and a percentile remark.
    """
    from app.schemas.market_dashboard import PriceChainNode, PriceChainResponse

    tiers: list[list[PriceChainNode]] = []

    for tier_pids in _PRICE_CHAIN_TIERS:
        tier_nodes: list[PriceChainNode] = []
        for pid in tier_pids:
            label = PRODUCT_LABELS.get(pid, {"en": pid, "zh": pid})

            # Full history
            history = _read_product_history_rows(pid)

            # Last 30 daily prices for sparkline
            sparkline = [p for _, p in history[-30:]] if history else []

            # Current price = last history value
            current_price = sparkline[-1] if sparkline else None

            # 30-day change pct
            change_pct_30d: Optional[float] = None
            if len(history) >= 2:
                # Compare last price to price ~30 days ago (or earliest if <30)
                compare_idx = max(0, len(history) - 31)
                old_price = history[compare_idx][1]
                if old_price and old_price != 0 and current_price:
                    change_pct_30d = round(
                        (current_price - old_price) / old_price * 100, 1
                    )

            # Percentile within ~3 years of data
            percentile: Optional[float] = None
            percentile_remark: Optional[str] = None
            if history and current_price is not None:
                all_prices = [p for _, p in history]
                if len(all_prices) >= 10:
                    below = sum(1 for p in all_prices if p < current_price)
                    percentile = round(below / len(all_prices) * 100, 1)
                    if percentile >= 90:
                        percentile_remark = f"At {percentile}th percentile in ~3 years — relatively high"
                    elif percentile <= 10:
                        percentile_remark = f"At {percentile}th percentile in ~3 years — relatively low"
                    else:
                        percentile_remark = f"At {percentile}th percentile in ~3 years — mid range"

            # Trend date range
            trend_start: Optional[str] = history[0][0] if history else None
            trend_end: Optional[str] = history[-1][0] if history else None

            tier_nodes.append(PriceChainNode(
                product_id=pid,
                name_en=label.get("en", pid),
                name_zh=label.get("zh", pid),
                current_price=round(current_price, 2) if current_price else None,
                change_pct_30d=change_pct_30d,
                sparkline=sparkline,
                percentile=percentile,
                percentile_remark=percentile_remark,
                trend_start_date=trend_start,
                trend_end_date=trend_end,
                group=label.get("group", "upstream"),
            ))
        tiers.append(tier_nodes)

    return PriceChainResponse(
        tiers=tiers,
        as_of_date=date.today().isoformat(),
    )
