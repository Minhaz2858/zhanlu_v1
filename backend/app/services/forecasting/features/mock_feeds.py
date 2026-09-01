"""Mock external-feed generator (Wave 3 T3.6).

Since all 3 Tier-3 feeds (T3.1 开工率, T3.2 库存, T3.3 进口价) are currently
BLOCKED (no data source confirmed), this module synthesises realistic
2-year weekly series so the entire pipeline — upload → store → load →
signal → feature → brief — can be exercised end-to-end before any real
feed arrives.

When real feeds land, this module is simply replaced by the same CSV
upload (or future API ingestion adapter) — loaders, signals, and the
rest of the pipeline are unchanged.

Realistic ranges (informed by industry observations):
- Operating rate (开工率): 50-90%
- Inventory (库存):       1000-8000 吨 (varies by product)
- Import price:           8-25 CNY/kg
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.models.forecasting import (
    EXTERNAL_FEED_DOMAINS,
    ForecastExternalPoint,
    ForecastExternalSeries,
)
from app.services.forecasting.features.external_feed_ingest import IngestError

logger = logging.getLogger(__name__)


# Documented realistic ranges — used by tests to assert mock output is sensible.
MOCK_OPERATING_RATE_RANGE: tuple[float, float] = (50.0, 90.0)
MOCK_INVENTORY_RANGE: tuple[float, float] = (1000.0, 8000.0)
MOCK_IMPORT_PRICE_RANGE: tuple[float, float] = (8.0, 25.0)


# ------------------------------------------------------------------ #
# generate_mock_series — pure function
# ------------------------------------------------------------------ #

def generate_mock_series(
    domain: str,
    product_key: str = "",
    n_weeks: int = 104,
    end_date: datetime | None = None,
    seed: int | None = None,
) -> pd.DataFrame:
    """Generate a synthetic weekly series for ``domain``.

    Parameters
    ----------
    domain : str
        ``operating_rate`` | ``inventory`` | ``import_price``. Unknown
        domains return an empty DataFrame.
    product_key : str
        Forecast product this series applies to (used for variability only).
    n_weeks : int
        Number of weekly observations to generate.
    end_date : datetime, optional
        Most-recent observation date. Defaults to ``datetime.now(timezone.utc)``.
    seed : int, optional
        RNG seed for reproducibility (default: deterministic per product_key).

    Returns
    -------
    pd.DataFrame with columns ``['date', <metric>]`` (sorted ascending).
    """
    if domain not in EXTERNAL_FEED_DOMAINS:
        logger.warning("generate_mock_series: unknown domain '%s'", domain)
        return pd.DataFrame()

    # Deterministic seed per product_key+domain for reproducibility
    if seed is None:
        seed = abs(hash((domain, product_key))) % (2**31)
    rng = np.random.default_rng(seed)

    end = end_date or (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    # Generate weekly dates ending at end_date (going back n_weeks)
    dates = [end - timedelta(weeks=i) for i in range(n_weeks - 1, -1, -1)]

    # Add a long-cycle sinusoid for trend + small noise (variability ~10% of range)
    weeks_axis = np.arange(n_weeks)
    long_cycle = np.sin(weeks_axis / 26.0 * 2 * math.pi)  # 6-month cycle
    short_cycle = np.sin(weeks_axis / 4.0 * 2 * math.pi)  # 1-month cycle
    noise = rng.normal(0, 1, size=n_weeks) * 0.05

    if domain == "operating_rate":
        lo, hi = MOCK_OPERATING_RATE_RANGE
        mid = (lo + hi) / 2
        amp = (hi - lo) / 2 * 0.6
        values = mid + amp * (0.5 * long_cycle + 0.5 * short_cycle) + amp * noise
        col = "op_rate"
    elif domain == "inventory":
        lo, hi = MOCK_INVENTORY_RANGE
        mid = (lo + hi) / 2
        amp = (hi - lo) / 2 * 0.5
        values = mid + amp * long_cycle + amp * noise
        col = "inventory_t"
    elif domain == "import_price":
        lo, hi = MOCK_IMPORT_PRICE_RANGE
        mid = (lo + hi) / 2
        amp = (hi - lo) / 2 * 0.5
        # Slow upward drift (oil-linked) + small cycle + noise
        drift = np.linspace(0, 0.4, n_weeks)
        values = mid + amp * (0.5 * long_cycle + drift) + amp * noise
        col = "import_price_cny"
    else:
        return pd.DataFrame()

    # Clip to documented ranges (the noise+drift can push slightly outside)
    values = np.clip(values, lo, hi)

    df = pd.DataFrame({"date": dates, col: np.around(values, 3)})
    return df


# ------------------------------------------------------------------ #
# seed_mock_feeds — writes to DB
# ------------------------------------------------------------------ #

def seed_mock_feeds(
    db: Session,
    product_key: str = "",
    n_weeks: int = 104,
    end_date: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Seed all 3 Wave 3 external-feed domains with mock data.

    Idempotent: re-running with the same product_key replaces existing
    rows for those series (upsert by series_key).

    Returns
    -------
    dict mapping domain → series summary::

        {
            "operating_rate": {"series_key": "mock_op_rate_<product>",
                                "row_count": 104, ...},
            "inventory": {...},
            "import_price": {...},
        }
    """
    end = end_date or (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    results: dict[str, dict[str, Any]] = {}

    for domain in EXTERNAL_FEED_DOMAINS:
        df = generate_mock_series(
            domain=domain, product_key=product_key,
            n_weeks=n_weeks, end_date=end,
        )
        if df.empty:
            continue

        series_key = f"mock_{domain.replace('_', '_')}_{product_key}"
        # Shorten the domain part for readability: "op_rate"/"inv"/"import_price"
        if domain == "operating_rate":
            short = "op_rate"
        elif domain == "inventory":
            short = "inv"
        else:
            short = domain
        series_key = f"mock_{short}_{product_key}"

        # Determine metric column name (must match the loader's METRIC_COLUMN)
        metric_col_map = {
            "operating_rate": "op_rate",
            "inventory": "inventory_t",
            "import_price": "import_price_cny",
        }
        col = metric_col_map[domain]

        # Find or create the series
        series = db.query(ForecastExternalSeries).filter_by(
            series_key=series_key,
        ).first()
        if series is None:
            series = ForecastExternalSeries(
                series_key=series_key,
                domain=domain,
                product_key=product_key,
                source="mock",
                cadence="weekly",
                uploaded_by="mock_feeds",
                notes=f"Synthetic mock data for {domain} ({n_weeks} weeks)",
            )
            db.add(series)
            db.flush()
        else:
            # Mark as mock source again in case it was previously csv_upload
            series.source = "mock"
            series.product_key = product_key

        # Upsert points
        rows_inserted = 0
        rows_updated = 0
        for _, row in df.iterrows():
            d = row["date"]
            if isinstance(d, pd.Timestamp):
                d = d.to_pydatetime()
            v = float(row[col])

            existing = db.query(ForecastExternalPoint).filter_by(
                series_id=series.id, date=d,
            ).first()
            if existing is None:
                db.add(ForecastExternalPoint(series_id=series.id, date=d, value=v))
                rows_inserted += 1
            else:
                existing.value = v
                rows_updated += 1

        db.flush()
        db.commit()

        # Update roll-up stats
        series.row_count = db.query(ForecastExternalPoint).filter_by(
            series_id=series.id,
        ).count()
        last_point = db.query(ForecastExternalPoint).filter_by(
            series_id=series.id,
        ).order_by(ForecastExternalPoint.date.desc()).first()
        series.last_value_date = last_point.date if last_point else None
        db.commit()
        db.refresh(series)

        results[domain] = {
            "series_id": series.id,
            "series_key": series.series_key,
            "domain": domain,
            "product_key": series.product_key,
            "row_count": series.row_count,
            "rows_inserted": rows_inserted,
            "rows_updated": rows_updated,
            "last_value_date": (
                series.last_value_date.isoformat()
                if series.last_value_date else None
            ),
        }
        logger.info(
            "seed_mock_feeds: domain=%s series=%s inserted=%d updated=%d total=%d",
            domain, series_key, rows_inserted, rows_updated, series.row_count,
        )

    return results