"""CSV ingestion + CRUD for Wave 3 external-feed store (T3.0).

The forecast pipeline NEVER reads directly from external APIs or files — it
queries ``forecast_external_points`` via the loader classes. This module is
the only path that writes into that store, which makes the rest of the
pipeline completely feed-agnostic.

When a new external feed (e.g. a real 隆众 开工率 API) becomes available, the
onboarding story is:

    1. Customer uploads a one-time CSV via ``POST /external-feeds`` to
       populate the store immediately.
    2. A separate ingestion adapter (future work) periodically fetches the
       same data from the API and writes into the SAME store via this
       same module — loaders never change.

This is the architectural pattern that makes feeds pluggable with zero code
changes to the forecasting pipeline.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, BinaryIO

import pandas as pd
from sqlalchemy.orm import Session

from app.models.forecasting import (
    EXTERNAL_FEED_DOMAINS,
    ForecastExternalPoint,
    ForecastExternalSeries,
)

logger = logging.getLogger(__name__)


class IngestError(ValueError):
    """Raised when a CSV upload cannot be ingested.

    Messages are user-facing — they are returned verbatim by the API endpoint
    so the customer knows what's wrong with their file.
    """


# ------------------------------------------------------------------ #
# Validation helpers
# ------------------------------------------------------------------ #

def _validate_domain(domain: str) -> None:
    if domain not in EXTERNAL_FEED_DOMAINS:
        raise IngestError(
            f"Unknown domain '{domain}'. Allowed: "
            f"{', '.join(EXTERNAL_FEED_DOMAINS)}"
        )


def _validate_series_key(series_key: str) -> None:
    if not series_key or not series_key.strip():
        raise IngestError("series_key is required and cannot be blank")
    if len(series_key) > 150:
        raise IngestError(
            f"series_key too long ({len(series_key)} > 150 chars)"
        )


def _parse_csv(file: BinaryIO) -> pd.DataFrame:
    """Read CSV from a file-like object and return a validated DataFrame.

    Required columns: ``date``, ``value``.  Optional: ``metadata`` (JSON
    string) or any other columns are stored as point metadata.
    """
    try:
        raw = file.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = raw
    except Exception as exc:
        raise IngestError(f"Failed to read file: {exc}") from exc

    if not text.strip():
        raise IngestError("CSV is empty")

    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as exc:
        raise IngestError(f"Failed to parse CSV: {exc}") from exc

    if df.empty:
        raise IngestError("CSV has no rows (header only or empty body)")

    # Normalise column names (lowercase + strip)
    df.columns = [str(c).strip().lower() for c in df.columns]

    if "date" not in df.columns:
        raise IngestError(
            "CSV must contain a 'date' column. "
            f"Found columns: {', '.join(df.columns)}"
        )
    if "value" not in df.columns:
        raise IngestError(
            "CSV must contain a 'value' column. "
            f"Found columns: {', '.join(df.columns)}"
        )

    # Validate dates
    parsed_dates = pd.to_datetime(df["date"], errors="coerce")
    bad_date_mask = parsed_dates.isna()
    if bad_date_mask.any():
        bad_examples = df.loc[bad_date_mask, "date"].head(3).tolist()
        raise IngestError(
            f"Invalid date values in 'date' column: {bad_examples}. "
            "Expected ISO format like '2025-01-01'."
        )
    df["date"] = parsed_dates

    # Validate numeric values
    parsed_values = pd.to_numeric(df["value"], errors="coerce")
    bad_value_mask = parsed_values.isna()
    if bad_value_mask.any():
        bad_examples = df.loc[bad_value_mask, "value"].head(3).tolist()
        raise IngestError(
            f"Non-numeric values in 'value' column: {bad_examples}"
        )
    df["value"] = parsed_values.astype(float)

    # Metadata: capture any extra columns into a JSON dict per row.
    extra_cols = [c for c in df.columns if c not in ("date", "value")]
    if extra_cols:
        df["__metadata__"] = df[extra_cols].apply(
            lambda row: {k: (None if pd.isna(v) else v) for k, v in row.items()},
            axis=1,
        )
    else:
        df["__metadata__"] = None

    return df


# ------------------------------------------------------------------ #
# ingest_csv — the main public entry point
# ------------------------------------------------------------------ #

def ingest_csv(
    *,
    db: Session,
    file: BinaryIO,
    series_key: str,
    domain: str,
    product_key: str | None = None,
    unit: str | None = None,
    cadence: str | None = None,
    uploaded_by: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Ingest a CSV into the external-feed store.

    Args:
        db: SQLAlchemy session (caller manages transaction).
        file: File-like object (e.g. ``UploadFile.file`` or ``BytesIO``).
        series_key: Unique feed identifier within org.
        domain: ``operating_rate`` | ``inventory`` | ``import_price``.
        product_key: Optional forecast product the feed applies to.
        unit: Optional unit string (e.g. ``%``, ``吨``, ``CNY/kg``).
        cadence: Optional cadence (``daily`` | ``weekly`` | ``monthly``).
        uploaded_by: User ID / system component that created the series.
        notes: Optional free-form notes.

    Returns:
        Summary dict::

            {
                "series_id": "...",
                "series_key": "...",
                "domain": "...",
                "product_key": "...",
                "unit": "...",
                "row_count": N,        # total points now in this series
                "rows_inserted": N,    # new points created by this upload
                "rows_updated": N,     # existing dates overwritten
                "last_value_date": "...",  # ISO datetime
            }

    Raises:
        IngestError: any validation failure (message is user-facing).
    """
    _validate_series_key(series_key)
    _validate_domain(domain)

    df = _parse_csv(file)

    # Find or create the parent series
    series = db.query(ForecastExternalSeries).filter_by(
        series_key=series_key,
    ).first()
    if series is None:
        series = ForecastExternalSeries(
            series_key=series_key,
            domain=domain,
            product_key=product_key,
            unit=unit,
            source="csv_upload",
            cadence=cadence,
            uploaded_by=uploaded_by,
            notes=notes,
        )
        db.add(series)
        db.flush()  # assign series.id
    else:
        # Series exists — update mutable metadata fields if provided
        if product_key is not None:
            series.product_key = product_key
        if unit is not None:
            series.unit = unit
        if cadence is not None:
            series.cadence = cadence
        if notes is not None:
            series.notes = notes

    rows_inserted = 0
    rows_updated = 0
    for _, row in df.iterrows():
        point_date: datetime = row["date"].to_pydatetime()
        point_value: float = float(row["value"])
        meta = row["__metadata__"]

        existing = db.query(ForecastExternalPoint).filter_by(
            series_id=series.id, date=point_date,
        ).first()
        if existing is None:
            db.add(ForecastExternalPoint(
                series_id=series.id,
                date=point_date,
                value=point_value,
                metadata_=meta,
            ))
            rows_inserted += 1
        else:
            existing.value = point_value
            existing.metadata_ = meta
            rows_updated += 1

    # Flush so the new points are visible to subsequent queries, then commit
    db.flush()
    db.commit()

    # Update roll-up stats (after commit so count() reflects persisted rows)
    series.row_count = db.query(ForecastExternalPoint).filter_by(
        series_id=series.id,
    ).count()
    last_point = db.query(ForecastExternalPoint).filter_by(
        series_id=series.id,
    ).order_by(ForecastExternalPoint.date.desc()).first()
    series.last_value_date = last_point.date if last_point else None

    db.commit()
    db.refresh(series)

    logger.info(
        "ingest_csv: series=%s domain=%s inserted=%d updated=%d total=%d",
        series_key, domain, rows_inserted, rows_updated, series.row_count,
    )

    return {
        "series_id": series.id,
        "series_key": series.series_key,
        "domain": series.domain,
        "product_key": series.product_key,
        "unit": series.unit,
        "row_count": series.row_count,
        "rows_inserted": rows_inserted,
        "rows_updated": rows_updated,
        "last_value_date": (
            series.last_value_date.isoformat()
            if series.last_value_date else None
        ),
    }


# ------------------------------------------------------------------ #
# CRUD: list_series / get_series_points / delete_series
# ------------------------------------------------------------------ #

def list_series(db: Session) -> list[dict[str, Any]]:
    """Return all series as dicts (id, series_key, domain, row_count, etc.)."""
    rows = db.query(ForecastExternalSeries).order_by(
        ForecastExternalSeries.created_date.desc(),
    ).all()
    return [r.to_dict() for r in rows]


def get_series_points(
    db: Session, series_key: str,
) -> pd.DataFrame:
    """Return points for a series as a DataFrame with columns ``['date', 'value']``.

    Mirrors the shape of ``ErpVolumeLoader.load()`` (date + metric column)
    so downstream code can use the same DataFrame-handling paths.
    """
    series = db.query(ForecastExternalSeries).filter_by(
        series_key=series_key,
    ).first()
    if series is None:
        return pd.DataFrame(columns=["date", "value"])

    points = db.query(ForecastExternalPoint).filter_by(
        series_id=series.id,
    ).order_by(ForecastExternalPoint.date).all()

    if not points:
        return pd.DataFrame(columns=["date", "value"])

    return pd.DataFrame([
        {"date": p.date, "value": float(p.value)}
        for p in points
    ])


def delete_series(db: Session, series_key: str) -> None:
    """Delete a series and cascade-delete its points. No-op if absent."""
    series = db.query(ForecastExternalSeries).filter_by(
        series_key=series_key,
    ).first()
    if series is None:
        return
    db.query(ForecastExternalPoint).filter_by(
        series_id=series.id,
    ).delete()
    db.delete(series)
    db.commit()