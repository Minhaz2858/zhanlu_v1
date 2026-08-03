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
from app.schemas.market_dashboard import MarketSnapshot, PriceHistoryPoint

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
    "isoprene":       "ecisco.isoprene",
    "piperylene":     "ecisco.piperylene",
    "dcpd":           "ecisco.dcpd",
    "raffinate_c5":   "ecisco.raffinate_c5",
    "sis":            "ecisco.sis",
    "c5_resin":       "ecisco.c5_resin",
    "blowing_agent":  "ecisco.blowing_agent",
    "styrene":        "ecisco.styrene",
}


def get_forecast_base_batch_v2(
    product_ids: list[str], day: int = 7, db=None
) -> ForecastBaseBatchResponse:
    """Read forecast base prices from the local ForecastRun table.

    Replaces the v1 reader that hit EDIA's stale v_forecast_price view.
    Returns the latest `results['{day}d']['base'][0]` per product, or
    None for products with no run yet.

    The forecast_runs table is populated by the nightly automation task
    (cron 0 2 * * *) which calls ForecastEngine.compute_all().
    """
    from app.models.forecasting import ForecastTarget, ForecastRun

    out: dict = {pid: None for pid in product_ids}
    horizon_key = f"{day}d"  # results JSON uses "3d"/"7d"/"30d" keys

    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        # Build product_id → product_key map for the requested IDs
        pid_to_key = {
            pid: PRODUCT_FORECAST_TARGET_KEY[pid]
            for pid in product_ids
            if pid in PRODUCT_FORECAST_TARGET_KEY
        }
        if not pid_to_key:
            return ForecastBaseBatchResponse(day=day, base_by_product=out)

        # Bulk-load targets by product_key
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
            horizon_payload = run.results.get(horizon_key) or {}
            base = horizon_payload.get("base")
            if base is None:
                continue
            # `base` is a list (from pd.Series.tolist()); take first point
            if isinstance(base, list) and len(base) > 0:
                out[pid] = float(base[0])
            elif isinstance(base, (int, float)):
                out[pid] = float(base)
    finally:
        if owns_session:
            db.close()

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

    # 3. Forecast 7-day base per product.
    forecast_resp = get_forecast_base_batch(pids, day=forecast_day)

    # 4. FX (static for v1).
    fx = get_fx_rate()

    bundle = OverviewBundle(
        snapshot=snap,
        regional=regional,
        regional_errors=regional_errors,
        forecast_day=forecast_day,
        forecast_base_by_product=forecast_resp.base_by_product,
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
