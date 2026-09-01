"""T2.1 Decision-ROI loop closure — nightly scoring + backfill.

Wraps the Wave 0 accuracy_tracker.score_pending_decisions() with idempotency,
error isolation, and the critical _backfill_actual_price_t() helper that fills
NULL actual_price_t from the external MySQL (so historical logs become scorable).
"""
import logging
from datetime import date

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _resolve_mysql_engine() -> object | None:
    """Resolve a MySQL engine for ERP data queries.

    Returns an engine or None.  Tries the exogenous_loaders'
    _resolve_mysql_engine (the canonical implementation), then falls back to
    importing mysql_data_source (where accuracy_tracker expects it).  If neither
    works, returns None (callers must tolerate missing MySQL).
    """
    try:
        from app.services.forecasting.features.exogenous_loaders import (
            _resolve_mysql_engine as _resolver,
        )
        return _resolver()
    except Exception:
        pass

    try:
        from app.services.forecasting.mysql_data_source import _resolve_mysql_engine
        return _resolve_mysql_engine()
    except Exception:
        pass

    try:
        from app.core.mysql_db import get_mysql_engine
        return get_mysql_engine()
    except Exception:
        pass

    return None


def _backfill_actual_price_t(db: Session) -> int:
    """Find ForecastDecisionLog rows where actual_price_t IS NULL and fill the
    price from the external MySQL sale_erp_v_{product_id}_data table (as_of_date).

    Returns the number of rows backfilled.  Graceful when MySQL is unavailable.
    """
    mysql = _resolve_mysql_engine()
    if mysql is None:
        logger.info("_backfill_actual_price_t: no MySQL engine — skipping")
        return 0

    from app.models.forecasting import ForecastDecisionLog

    # Find NULL-priced logs, grouped by product to reduce queries
    null_rows = (
        db.query(ForecastDecisionLog)
        .filter(ForecastDecisionLog.actual_price_t.is_(None))
        .all()
    )
    if not null_rows:
        return 0

    count = 0
    for row in null_rows:
        try:
            table = f"sale_erp_v_{row.product_id}_data"
            stmt = sa_text(
                f"SELECT date, price FROM {table} "
                f"WHERE date = :dt AND price IS NOT NULL "
                f"ORDER BY date DESC LIMIT 1"
            )
            with mysql.connect() as conn:
                result = conn.execute(stmt, {"dt": row.as_of_date})
                r = result.fetchone()
                if r is not None:
                    row.actual_price_t = float(r[1])
                    count += 1
            # per-row flush so a single bad row doesn't abort the batch
            db.flush()
        except Exception:
            logger.warning(
                "_backfill_actual_price_t: error for %s as_of=%s",
                row.product_id,
                row.as_of_date,
                exc_info=True,
            )
    return count


def run_decision_scoring(db: Session) -> dict:
    """Score pending decision logs whose horizon has closed.

    Wraps accuracy_tracker.score_pending_decisions() with:
    - _backfill_actual_price_t() pre-pass to fill NULL entry prices
    - Error isolation (one bad log does not abort the batch)
    - Idempotency (only scores realised=True logs)

    Returns:
        {scored_count, total_roi_avg, buy_count, sell_count, accuracy_pct, errors}
    """
    errors: list[str] = []

    # 1. Pre-pass: backfill missing actual_price_t
    try:
        backfilled = _backfill_actual_price_t(db)
        logger.info("run_decision_scoring: backfilled=%d", backfilled)
    except Exception as exc:
        errors.append(f"backfill_error: {exc}")
        logger.warning("run_decision_scoring: backfill failed", exc_info=True)

    # 2. Forward to existing score_pending_decisions
    try:
        from app.services.forecasting.accuracy_tracker import (
            score_pending_decisions,
        )
        result = score_pending_decisions(db)
    except ImportError:
        errors.append("score_pending_decisions import failed")
        result = {"scored_count": 0}
    except Exception as exc:
        errors.append(f"scoring_error: {exc}")
        logger.warning("run_decision_scoring: scoring failed", exc_info=True)
        result = {"scored_count": 0}

    result.setdefault("errors", [])
    result["errors"].extend(errors)
    return result
