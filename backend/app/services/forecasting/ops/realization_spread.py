"""Realization spread model — learn quotation-vs-ERP execution price gap.

spread = median(erp_execution_price / lz_quotation_price) over trailing N days.

Stored in target.model_config["realization_spread"] (ratio, e.g. 0.97).
At decision time: adjusted_forecast = forecast * (1 - spread).

This corrects the systematic gap between published market quotation
(md_t_lz_price) and actual deal prices in the ERP (sale_erp_v_*_data).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.services.forecasting.features.exogenous_loaders import _ERP_TABLE_MAP

logger = logging.getLogger(__name__)

_SPREAD_WINDOW_DAYS: int = 90


def compute_realization_spread(
    db: Session,
    product_id: str,
    product_key: str,
    window_days: int = _SPREAD_WINDOW_DAYS,
) -> Optional[float]:
    """Compute median spread ratio for a product over the last N days.

    Returns None if insufficient data (fewer than 10 overlapping days).
    """
    table = _ERP_TABLE_MAP.get(product_id, f"sale_erp_v_{product_id}_data")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)

    # 1) ERP execution prices
    erp_sql = f"""
        SELECT date, price
        FROM {table}
        WHERE date >= %(start)s AND date <= %(end)s
        ORDER BY date
    """
    try:
        erp_df = pd.read_sql(
            erp_sql, db.bind, params={"start": start.date(), "end": end.date()}
        )
    except Exception as exc:
        logger.warning("realization-spread: ERP read failed for %s: %s", product_id, exc)
        return None

    if erp_df.empty:
        return None
    erp_df["date"] = pd.to_datetime(erp_df["date"])
    erp_df = erp_df.set_index("date").rename(columns={"price": "erp_price"})

    # 2) LZ quotation prices (from the product's datasource)
    from app.models.forecasting import ForecastTarget

    tgt = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if tgt is None or not tgt.datasource:
        logger.warning("realization-spread: no target/datasource for %s", product_key)
        return None

    ds = tgt.datasource
    quote_table = ds.get("table", "md_t_lz_price")
    quote_measure = ds.get("measure", "price")
    quote_filter = ds.get("filter", "")

    where_clauses = ["date >= %(start)s", "date <= %(end)s"]
    params = {"start": start.date(), "end": end.date()}
    if quote_filter:
        where_clauses.append(quote_filter)

    quote_sql = f"""
        SELECT date, {quote_measure} AS price
        FROM {quote_table}
        WHERE {" AND ".join(where_clauses)}
        ORDER BY date
    """
    try:
        quote_df = pd.read_sql(quote_sql, db.bind, params=params)
    except Exception as exc:
        logger.warning("realization-spread: quotation read failed for %s: %s", product_key, exc)
        return None

    if quote_df.empty:
        return None
    quote_df["date"] = pd.to_datetime(quote_df["date"])
    quote_df = quote_df.set_index("date").rename(columns={"price": "quote_price"})

    # 3) Align and compute ratio
    merged = pd.merge(erp_df, quote_df, left_index=True, right_index=True, how="inner")
    merged = merged[
        (merged["erp_price"] > 0) & (merged["quote_price"] > 0)
    ]
    if len(merged) < 10:
        logger.info(
            "realization-spread: insufficient overlap for %s (%d days)",
            product_id, len(merged),
        )
        return None

    ratios = merged["erp_price"] / merged["quote_price"]
    spread = float(np.median(ratios))
    logger.info(
        "realization-spread for %s: %.4f (N=%d, std=%.4f)",
        product_id, spread, len(merged), float(ratios.std()),
    )
    return spread


def apply_realization_spread(
    forecast_value: float,
    spread: Optional[float],
) -> float:
    """Adjust a forecast by the realization spread.

    adjusted = forecast * (1 - spread)

    If spread is None, returns forecast unchanged.
    """
    if spread is None or not np.isfinite(spread):
        return forecast_value
    return forecast_value * (1.0 - spread)


def update_target_spread(
    db: Session,
    target,
    product_id: str,
    window_days: int = _SPREAD_WINDOW_DAYS,
) -> Optional[float]:
    """Compute and persist realization spread into target.model_config.

    Returns the computed spread or None.
    """
    spread = compute_realization_spread(
        db=db,
        product_id=product_id,
        product_key=target.product_key,
        window_days=window_days,
    )
    cfg = target.model_config or {}
    cfg["realization_spread"] = spread
    target.model_config = cfg
    db.commit()
    return spread
