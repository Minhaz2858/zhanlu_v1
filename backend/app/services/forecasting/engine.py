"""Forecasting Engine orchestrator.

The single entry point that chains the full forecasting pipeline:
discovery → quality → models → ensemble → guard → scenarios → cache.

Reads/writes Section 1 tables (ForecastTarget, ForecastRun,
ForecastAccuracyLog, ForecastBusinessRule).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models.forecasting import (
    ForecastAccuracyLog,
    ForecastBusinessRule,
    ForecastRun,
    ForecastTarget,
)
from app.services.db.query_service import QueryService
from app.services.forecasting.backtest import BacktestResult, evaluate
from app.services.forecasting.edia_source import EdiaMysqlDataSource
from app.services.forecasting.discovery import discover
from app.services.forecasting.ensemble import EnsembleResult, blend, run_models
from app.services.forecasting.guard import GuardResult, evaluate_guard
from app.services.forecasting.models import build_model_pool
from app.services.forecasting.quality import QualityResult, score_series
from app.services.forecasting.scenarios import ScenarioResult, generate

logger = logging.getLogger(__name__)

# Default seasonal period (weekly pattern for daily data)
_DEFAULT_SEASONAL_PERIOD = 7

# Retention: delete forecast_runs older than N days per target
_RETENTION_DAYS = 90


class ForecastEngine:
    """Orchestrator for the entire forecasting pipeline.

    Usage::

        engine = ForecastEngine(db)
        targets = engine.discover_and_register(kb_id)
        for t in targets:
            run = engine.compute_target(t.id)
    """

    # Map db_type → identifier-quoting style. Different SQL dialects use
    # different identifier quotes: MySQL uses backticks, Postgres uses
    # double quotes, SQL Server uses square brackets, etc. The previous
    # implementation hardcoded Postgres-style double quotes which broke
    # against MySQL.
    _IDENTIFIER_QUOTES: dict[str, tuple[str, str]] = {
        "mysql":     ("`", "`"),
        "mariadb":   ("`", "`"),
        "postgres":  ('"', '"'),
        "postgresql":('"', '"'),
        "sqlite":    ('"', '"'),
        "mssql":     ("[", "]"),
        "sqlserver": ("[", "]"),
        "oracle":    ('"', '"'),
    }

    @classmethod
    def _quote_identifier(cls, name: str, db_type: str | None) -> str:
        """Quote an SQL identifier for the given db_type. Falls back to
        double quotes (Postgres style) for unknown dialects."""
        if not name:
            return name
        open_q, close_q = cls._IDENTIFIER_QUOTES.get(
            (db_type or "").lower(), ('"', '"')
        )
        return f"{open_q}{name}{close_q}"

    def __init__(self, db: Session) -> None:
        self._db = db

    # ── Discovery ──────────────────────────────────────────────────

    def discover_and_register(
        self,
        kb_id: str,
        org_id: str = "default-org",
        app_id: str = "default-app",
        max_tables: int = 50,
    ) -> list[ForecastTarget]:
        """Run discovery and register new candidates as ForecastTarget rows.

        Existing targets (matched by table + time_column + measure +
        dimensions) are skipped.
        """
        candidates = discover(self._db, kb_id, max_tables=max_tables)
        new_targets: list[ForecastTarget] = []

        for cand in candidates:
            # Build datasource descriptor
            datasource = {
                "table": cand["table"],
                "time_column": cand["time_column"],
                "measure": cand["measure"],
                "dimensions": cand.get("dimensions", []),
                "granularity": cand["granularity"],
            }
            name = f"{cand['table']}.{cand['measure']}"
            dim_str = ",".join(cand.get("dimensions", []))
            if dim_str:
                name += f"[{dim_str}]"

            # Check if already registered
            existing = (
                self._db.query(ForecastTarget)
                .filter(
                    ForecastTarget.product_key == name,
                    ForecastTarget.org_id == org_id,
                    ForecastTarget.is_deleted == False,
                )
                .first()
            )
            if existing:
                logger.debug("Target %s already registered, skipping", name)
                continue

            target = ForecastTarget(
                org_id=org_id,
                app_id=app_id,
                product_key=name,
                name=name,
                datasource=datasource,
                level=0,
                status="discovered",
                source="discovery",
                include_in_weekly_report=False,
            )
            self._db.add(target)
            new_targets.append(target)

        if new_targets:
            self._db.flush()
            logger.info(
                "Discovered and registered %d new targets", len(new_targets)
            )

        return new_targets

    # ── Compute one target ─────────────────────────────────────────

    def compute_target(
        self,
        target_id: str,
        horizons: list[int] | None = None,
        seasonal_period: int = _DEFAULT_SEASONAL_PERIOD,
    ) -> ForecastRun | None:
        """Full pipeline for a single ForecastTarget.

        1. Load target + pull time series from datasource
        2. Score quality → update target
        3. Backtest all models → per-model error
        4. Fit all models + ensemble blend
        5. Honesty gate check
        6. Generate scenarios
        7. Write ForecastRun + ForecastAccuracyLog
        8. Apply business rules (if any active)
        """
        if horizons is None:
            horizons = [3, 7, 30]

        target = self._db.query(ForecastTarget).filter(
            ForecastTarget.id == target_id,
            ForecastTarget.is_deleted == False,
        ).first()
        if not target:
            logger.warning("ForecastTarget %s not found", target_id)
            return None

        # Pull time series from datasource
        y = self._fetch_series(target)
        if y is None or len(y.dropna()) < 2:
            logger.warning("Target %s: insufficient data", target_id)
            return None

        # --- Quality scoring ---
        qr = score_series(y)
        target.quality_grade = qr.grade
        target.quality_stats = qr.stats
        target.status = "active" if qr.grade in ("A", "B") else target.status
        self._db.flush()

        # --- Build model pool ---
        granularity = (target.datasource or {}).get("granularity", "daily")
        sp = _seasonal_period_for_granularity(granularity, seasonal_period)
        models = build_model_pool(seasonal_period=sp)

        # --- Backtest ---
        bt = evaluate(y, models, seasonal_period=sp)
        per_model_mape = bt.per_model_mape

        # --- Fit + forecast ---
        h_max = max(horizons)
        forecasts, models_run, models_failed = run_models(
            models, y, h=h_max, seasonal_period=sp
        )

        # --- Ensemble ---
        ensemble_result = blend(forecasts, per_model_mape)

        # --- Guard ---
        naive_fc = forecasts.get("seasonal_naive")
        if naive_fc is None:
            # Fallback: run SeasonalNaive manually
            from app.services.forecasting.models.naive import SeasonalNaive
            sn = SeasonalNaive(seasonal_period=sp)
            try:
                sn.fit(y, seasonal_period=sp)
                naive_fc = sn.forecast(h_max)
            except Exception:
                naive_fc = ensemble_result.point_forecast

        guard_result = evaluate_guard(
            ensemble_result.point_forecast,
            naive_fc,
            bt.ensemble_mape,
            bt.naive_mape,
        )

        # --- Scenarios ---
        scenarios_result = generate(
            guard_result.published_forecast,
            residuals=bt.residuals,
            mape=bt.ensemble_mape,
            horizons=horizons,
        )

        # --- Build results JSON ---
        results = {
            str(h): {
                "base": scenarios_result.horizons[h]["base"].tolist(),
                "bull": scenarios_result.horizons[h]["bull"].tolist(),
                "bear": scenarios_result.horizons[h]["bear"].tolist(),
            }
            for h in horizons
        }

        model_detail = {
            "models_run": models_run,
            "models_failed": models_failed,
            "weights": ensemble_result.weights,
            "ensemble_mape": bt.ensemble_mape,
            "naive_mape": bt.naive_mape,
            "metric": bt.metric,
        }

        # --- Write ForecastRun ---
        run = ForecastRun(
            target_id=target_id,
            org_id=target.org_id,
            app_id=target.app_id,
            results=results,
            below_naive_baseline=guard_result.below_naive_baseline,
            confidence=scenarios_result.confidence,
            as_of_date=datetime.now(timezone.utc),
            model_detail=model_detail,
        )
        self._db.add(run)

        # --- Write ForecastAccuracyLog ---
        for name, mape in per_model_mape.items():
            for h in horizons:
                log_entry = ForecastAccuracyLog(
                    target_id=target_id,
                    org_id=target.org_id,
                    app_id=target.app_id,
                    horizon_days=h,
                    mape=mape if mape < float("inf") else None,
                    naive_mape=bt.naive_mape
                    if bt.naive_mape < float("inf")
                    else None,
                    skill_vs_naive=(
                        (mape - bt.naive_mape)
                        if mape < float("inf") and bt.naive_mape < float("inf")
                        else None
                    ),
                    below_naive_baseline=guard_result.below_naive_baseline,
                    per_model=per_model_mape,
                )
                self._db.add(log_entry)

        self._db.flush()

        # --- Apply business rules ---
        # (Future: seasonal adjustments, event overrides, guardrails)
        _apply_rules(self._db, target_id, run)

        return run

    # ── Nightly batch ──────────────────────────────────────────────

    def compute_all(
        self,
        org_id: str,
        horizons: list[int] | None = None,
        seasonal_period: int = _DEFAULT_SEASONAL_PERIOD,
    ) -> list[ForecastRun]:
        """Compute forecasts for all active targets in an org."""
        targets = (
            self._db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == org_id,
                ForecastTarget.status.in_(["active", "discovered"]),
                ForecastTarget.is_deleted == False,
            )
            .all()
        )

        runs: list[ForecastRun] = []
        for target in targets:
            try:
                run = self.compute_target(
                    target.id,
                    horizons=horizons,
                    seasonal_period=seasonal_period,
                )
                if run:
                    runs.append(run)
            except Exception as exc:
                logger.error(
                    "Failed to compute target %s: %s", target.id, exc
                )

        # Retention: delete old runs
        _purge_old_runs(self._db, org_id, days=_RETENTION_DAYS)

        return runs

    # ── Read cached forecast ───────────────────────────────────────

    def get_forecast(self, target_id: str) -> dict | None:
        """Return the most recent cached ForecastRun for a target."""
        run = (
            self._db.query(ForecastRun)
            .filter(
                ForecastRun.target_id == target_id,
                ForecastRun.is_deleted == False,
            )
            .order_by(ForecastRun.created_date.desc())
            .first()
        )
        if not run:
            return None
        return {
            "results": run.results,
            "below_naive_baseline": run.below_naive_baseline,
            "confidence": run.confidence,
            "as_of_date": run.as_of_date.isoformat() if run.as_of_date else None,
            "model_detail": run.model_detail,
        }

    def get_accuracy(self, target_id: str) -> list[dict]:
        """Return all ForecastAccuracyLog entries for a target."""
        logs = (
            self._db.query(ForecastAccuracyLog)
            .filter(
                ForecastAccuracyLog.target_id == target_id,
                ForecastAccuracyLog.is_deleted == False,
            )
            .order_by(ForecastAccuracyLog.created_date.desc())
            .all()
        )
        return [
            {
                "horizon_days": log.horizon_days,
                "mape": log.mape,
                "naive_mape": log.naive_mape,
                "skill_vs_naive": log.skill_vs_naive,
                "below_naive_baseline": log.below_naive_baseline,
                "per_model": log.per_model,
            }
            for log in logs
        ]

    # ── Datasource fetch ───────────────────────────────────────────

    def _fetch_series(self, target: ForecastTarget) -> pd.Series | None:
        """Pull the raw time series from the user's datasource."""
        ds = target.datasource or {}

        # ── EDIA MySQL branch ─────────────────────────────────────
        # When the datasource descriptor names edia_mysql as the source,
        # read directly from the EDIA MySQL mirror via EdiaMysqlDataSource.
        # This bypasses the KB/QueryService path entirely and is used by
        # the nightly forecast scheduler for the 12 dashboard products.
        if ds.get("source") == "edia_mysql":
            try:
                src = EdiaMysqlDataSource()
                df = src.read_history(ds)
                # Convert DataFrame to Series indexed by datetime, as the
                # downstream pipeline (quality → models → ensemble) expects.
                return pd.Series(df["y"].values, index=df["ds"])
            except Exception as exc:
                logger.warning(
                    "EdiaMysqlDataSource failed for target %s: %s",
                    target.id, exc,
                )
                return None

        # ── Existing KB-backed path (unchanged) ──────────────────
        table = ds.get("table")
        tc = ds.get("time_column")
        mc = ds.get("measure")
        dims = ds.get("dimensions", [])
        # Prefer kb_id from datasource; fall back to org_id for legacy targets.
        kb_id = ds.get("kb_id") or target.org_id

        if not table or not tc or not mc:
            logger.warning("Target %s: missing datasource config", target.id)
            return None

        # Look up the KB to learn its db_type so we can quote identifiers
        # correctly (MySQL needs backticks, Postgres needs double quotes, etc.).
        from app.models.knowledge_base import KnowledgeBase
        kb = self._db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        db_type = (kb.db_type if kb else None) or None

        q = lambda n: self._quote_identifier(n, db_type)  # noqa: E731

        query_svc = QueryService(self._db)

        dim_selects = ", ".join(q(d) for d in dims)
        dim_group = ", ".join(q(d) for d in dims) if dims else ""

        group_clause = q(tc)
        if dim_group:
            group_clause += f", {dim_group}"

        sql = (
            f'SELECT {q(tc)} AS t, '
            f'{dim_selects + ", " if dim_selects else ""}'
            f'AVG({q(mc)}) AS y '
            f'FROM {q(table)} '
            f'WHERE {q(tc)} IS NOT NULL AND {q(mc)} IS NOT NULL '
            f"GROUP BY {group_clause} "
            f"ORDER BY t"
        )

        try:
            result = query_svc.execute(kb_id, sql, timeout_s=30)
        except Exception as exc:
            logger.error("Failed to fetch series for target %s: %s", target.id, exc)
            return None

        rows = result.get("rows", [])
        if not rows:
            return None

        df = pd.DataFrame(rows)
        if "t" not in df.columns or "y" not in df.columns:
            return None

        y = df.set_index("t")["y"].astype(float)
        return y

    # ── Truth-anchor variants (Task 4) ─────────────────────────────

    def compute_target_anchored(
        self,
        target_id: str,
        horizons: list[int] | None = None,
        seasonal_period: int = _DEFAULT_SEASONAL_PERIOD,
    ) -> dict | None:
        """Like compute_target(), but also returns the source data for truth-gating.

        Returns:
            None if target not found.
            {
                "run": ForecastRun | None,    # the engine's run, or None on compute failure
                "source_table": str,           # the datasource table
                "sample_size": int,            # number of non-null rows
                "anchor_rows": list[dict],     # first/last rows for LLM inspection
            }
        """
        target = self._db.query(ForecastTarget).filter(
            ForecastTarget.id == target_id,
            ForecastTarget.is_deleted == False,
        ).first()
        if not target:
            return None

        ds = target.datasource or {}
        source_table = ds.get("table", "unknown")

        # Pull raw series and capture anchor before running the engine
        y = self._fetch_series(target)
        if y is None:
            return {
                "run": None,
                "source_table": source_table,
                "sample_size": 0,
                "anchor_rows": [],
            }

        clean = y.dropna()
        sample_size = len(clean)
        seen_dates: set[str] = set()
        anchor_rows: list[dict] = []
        for idx, val in clean.head(10).items():
            d = str(idx.date())
            seen_dates.add(d)
            anchor_rows.append({"date": d, "value": float(val)})
        for idx, val in clean.tail(10).items():
            d = str(idx.date())
            if d not in seen_dates:
                anchor_rows.append({"date": d, "value": float(val)})

        # Now run the full pipeline. Be robust: a thin series may cause the
        # ensemble to raise (ValueError: no forecasts to blend). In that case
        # we still want to return the truth-anchor data so the LLM can report
        # insufficient_data correctly.
        try:
            run = self.compute_target(target_id, horizons, seasonal_period)
        except Exception as exc:
            logger.warning(
                "compute_target raised for %s: %s — returning anchor only",
                target_id, exc,
            )
            run = None
        return {
            "run": run,
            "source_table": source_table,
            "sample_size": sample_size,
            "anchor_rows": anchor_rows,
        }


# ── helpers ────────────────────────────────────────────────────────────

def _seasonal_period_for_granularity(
    granularity: str, default: int = 7
) -> int:
    """Map granularity string to a reasonable seasonal period."""
    mapping = {
        "daily": 7,
        "weekly": 4,
        "monthly": 12,
    }
    return mapping.get(granularity.lower(), default)


def _apply_rules(
    db: Session,
    target_id: str,
    run: ForecastRun,
) -> None:
    """Apply active business rules to the forecast (future hook).

    Currently a placeholder — reads rules from forecast_business_rules
    and adjusts the run's results JSON in-place with seasonal/causal
    adjustments.
    """
    rules = (
        db.query(ForecastBusinessRule)
        .filter(
            ForecastBusinessRule.target_id == target_id,
            ForecastBusinessRule.status == "active",
            ForecastBusinessRule.is_deleted == False,
        )
        .all()
    )
    if not rules:
        return

    # Future: apply rule_type=seasonal adjustments to the cached results
    logger.debug("Found %d active business rules for target %s", len(rules), target_id)


def _purge_old_runs(
    db: Session,
    org_id: str,
    days: int = _RETENTION_DAYS,
) -> int:
    """Soft-delete forecast_runs older than *days*."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = (
        db.query(ForecastRun)
        .filter(
            ForecastRun.org_id == org_id,
            ForecastRun.created_date < cutoff,
            ForecastRun.is_deleted == False,
        )
        .update({"is_deleted": True}, synchronize_session=False)
    )
    if count:
        logger.info("Purged %d old ForecastRun rows (retention=%d days)", count, days)
    return count
