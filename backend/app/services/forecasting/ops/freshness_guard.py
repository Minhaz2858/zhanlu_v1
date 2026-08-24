"""Data freshness guard — detect stale data, interpolate short gaps, alert on outages.

P2 improvement: ensures the forecasting engine never runs on stale data silently.

Behavior:
- freshness_check(target, series_df) → FreshnessReport
- If latest point is >3 days old → stale=True
- If gap is 1-3 days → interpolated (linear)
- If gap is >3 days → outage_alert=True (do NOT interpolate)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_DAYS: int = 3
_MAX_INTERPOLATE_GAP_DAYS: int = 3


@dataclass
class FreshnessReport:
    """Result of a freshness check on a price series."""

    product_key: str
    latest_date: Optional[datetime] = None
    days_since_latest: Optional[int] = None
    is_stale: bool = False
    gap_count: int = 0
    interpolated_count: int = 0
    outage_alert: bool = False
    interpolated_series: Optional[pd.DataFrame] = None
    notes: list[str] = field(default_factory=list)


def check_freshness(
    product_key: str,
    series_df: pd.DataFrame,
    date_col: str = "date",
    value_col: str = "price",
    stale_threshold_days: int = _STALE_THRESHOLD_DAYS,
    max_interpolate_gap_days: int = _MAX_INTERPOLATE_GAP_DAYS,
    now: Optional[datetime] = None,
) -> FreshnessReport:
    """Check data freshness and interpolate short gaps.

    Args:
        series_df: DataFrame with date_col and value_col.
        stale_threshold_days: Data older than this is flagged stale.
        max_interpolate_gap_days: Gaps up to this many days are interpolated.
        now: Reference time (default UTC now).

    Returns:
        FreshnessReport with stale flag, interpolation count, and alert status.
    """
    if series_df.empty:
        return FreshnessReport(
            product_key=product_key,
            is_stale=True,
            notes=["empty series"],
        )

    if now is None:
        now = datetime.now(timezone.utc)

    df = series_df.copy()
    df[date_col] = pd.to_datetime(df[date_col], utc=True)
    df = df.sort_values(date_col).reset_index(drop=True)

    latest = df[date_col].max()
    days_since = (now - latest).days

    report = FreshnessReport(
        product_key=product_key,
        latest_date=latest,
        days_since_latest=days_since,
        is_stale=days_since > stale_threshold_days,
    )

    if days_since > stale_threshold_days:
        report.notes.append(f"stale: latest data is {days_since} days old")

    # Detect gaps
    df["gap_days"] = df[date_col].diff().dt.days
    gaps = df[df["gap_days"] > 1]
    report.gap_count = len(gaps)

    if gaps.empty:
        report.interpolated_series = df[[date_col, value_col]]
        return report

    # Determine if any gap exceeds interpolation threshold
    max_gap = int(gaps["gap_days"].max())
    if max_gap > max_interpolate_gap_days:
        report.outage_alert = True
        report.notes.append(f"outage detected: max gap = {max_gap} days (>{max_interpolate_gap_days})")
        report.interpolated_series = df[[date_col, value_col]]
        return report

    # Interpolate short gaps (1-3 days)
    df_interp = df.set_index(date_col)[[value_col]].asfreq("D")
    before_count = df_interp[value_col].notna().sum()
    df_interp[value_col] = df_interp[value_col].interpolate(method="linear")
    after_count = df_interp[value_col].notna().sum()
    report.interpolated_count = after_count - before_count
    report.notes.append(f"interpolated {report.interpolated_count} missing day(s)")
    report.interpolated_series = df_interp.reset_index().rename(
        columns={"index": date_col}
    )
    return report


def flag_stale_targets(db, stale_threshold_days: int = _STALE_THRESHOLD_DAYS) -> list[str]:
    """Query all active ForecastTargets and return product_keys of stale ones.

    Intended for the nightly loop to skip stale targets or log warnings.
    """
    from app.models.forecasting import ForecastTarget

    stale_keys: list[str] = []
    targets = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.status == "active",
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for t in targets:
        cfg = t.model_config or {}
        last_freshness = cfg.get("data_freshness_at")
        if last_freshness:
            last_dt = pd.to_datetime(last_freshness, utc=True)
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since > stale_threshold_days:
                stale_keys.append(t.product_key)
    return stale_keys
