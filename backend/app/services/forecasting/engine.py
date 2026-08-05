"""Forecasting Engine orchestrator.

The single entry point that chains the full forecasting pipeline:
discovery → quality → models → ensemble → guard → scenarios → cache.

Reads/writes Section 1 tables (ForecastTarget, ForecastRun,
ForecastAccuracyLog, ForecastBusinessRule).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
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
from app.services.forecasting.accuracy_tracker import (
    adaptive_weights,
    detect_drift,
)
from app.services.forecasting.backtest import BacktestResult, evaluate
from app.services.forecasting.conformal import calibrate as conformal_calibrate
from app.services.forecasting.edia_source import EdiaMysqlDataSource
from app.services.forecasting.discovery import discover
from app.services.forecasting.ensemble import EnsembleResult, blend, run_models
from app.services.forecasting.explain import explain_forecast
from app.services.forecasting.features.feature_registry import (
    FeatureSpec,
    derive_feature_spec,
    topological_order,
)
from app.services.forecasting.features.feature_builder import build_features
from app.services.forecasting.guard import GuardResult, evaluate_guard
from app.services.forecasting.models import build_model_pool
from app.services.forecasting.preprocess import preprocess_series
from app.services.forecasting.quality import QualityResult, score_series
from app.services.forecasting.reconcile import (
    CoherenceReport,
    apply_coherence,
    check_coherence,
)
from app.services.forecasting.price_change_probability import (
    compute as compute_price_change_probability,
)
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
        as_of: datetime | None = None,
    ) -> ForecastRun | None:
        """Full pipeline for a single ForecastTarget.

        1. Load target + pull time series from datasource
        2. Score quality → update target
        3. Preprocessing (spike detection, stale-data guard, winsorization)
        4. Backtest all models → per-model error
        5. Exogenous feature building (KG-driven)
        6. Fit all models + ensemble blend (+ exog-aware models)
        7. Honesty gate check
        8. Value-chain coherence (spread guardrails)
        9. Driver attribution + drift detection
        10. Generate scenarios
        11. Write ForecastRun + ForecastAccuracyLog
        12. Apply business rules (if any active)

        as_of: When set, truncates the fetched time series to dates ≤ as_of
               and stamps the run with that date (walk-forward hindcast).
               Default None → live forecast (no truncation, now() stamp).
        """
        if horizons is None:
            horizons = [3, 7, 15, 30]

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

        # --- as_of: truncate series to the hindcast origin date ---
        if as_of is not None:
            as_of_ts = pd.Timestamp(as_of)
            # Normalise: ensure both sides are tz-naive for safe comparison
            if as_of_ts.tzinfo is not None:
                as_of_ts = as_of_ts.tz_convert(None)
            if y.index.tz is not None:
                y.index = y.index.tz_convert(None)
            y = y[y.index <= as_of_ts]
            if len(y.dropna()) < 2:
                logger.warning(
                    "Target %s: insufficient data after as_of truncation (as_of=%s, rows=%d)",
                    target_id, as_of.date().isoformat(), len(y),
                )
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
        models = build_model_pool(seasonal_period=sp, y=y)

        # --- Step 3: Preprocessing (Phase 1) ---
        preprocess_enabled = _get_config_bool("FORECAST_PREPROCESS_ENABLED", True)
        preprocess_cleaned = y
        cleaning_report = None
        if preprocess_enabled:
            try:
                pp = preprocess_series(preprocess_cleaned, target.product_key, seasonal_period=sp)
                preprocess_cleaned = pp.y_clean
                cleaning_report = pp.report
            except Exception as exc:
                logger.warning("Preprocess failed for %s: %s", target.product_key, exc)

        # Use cleaned series for backtest + models
        y_model = preprocess_cleaned

        # --- Backtest ---
        bt = evaluate(y_model, models, seasonal_period=sp, horizons=horizons)
        per_model_mape = bt.per_model_mape

        # --- Fit + forecast ---
        h_max = max(horizons)
        forecasts, models_run, models_failed = run_models(
            models, y_model, h=h_max, seasonal_period=sp
        )

        # --- Step 5: Exogenous features (Phase 1) ---
        exog_enabled = _get_config_bool("FORECAST_EXOG_ENABLED", True)
        feature_names: list[str] = []
        exog_spec: FeatureSpec | None = None
        exog_degraded = False
        features_used: list[str] = []
        coherence_report: CoherenceReport | None = None

        if exog_enabled:
            try:
                from app.config import settings
                from app.services.knowledge_graph.graph import build_c5_c9_graph
                from app.services.forecasting.features.exogenous_loaders import (
                    FeedstockLoader, FxLoader, EventFlagLoader, ErpVolumeLoader,
                    OperatingRateLoader, InventoryLoader, ImportPriceLoader,
                )

                kg = build_c5_c9_graph()
                exog_spec = derive_feature_spec(target.product_key, kg)
                feature_names = exog_spec.feedstock_keys

                # ERP volume exogenous feature (Wave 1, flag-gated)
                volume_df = None
                if os.environ.get("FORECAST_ERP_VOLUME_EXOG_ENABLED", "false").lower() == "true":
                    try:
                        parts = (target.product_key or "").split(".")
                        product_id = parts[1] if len(parts) >= 2 else (target.product_key or "")
                        vol_loader = ErpVolumeLoader(
                            product_id=product_id, lookback_days=365, org_id=target.org_id
                        )
                        volume_df = vol_loader.load()
                        if not volume_df.empty:
                            volume_df = volume_df.set_index("date")
                            logger.info(
                                "ERP volume loaded for %s: %d days", target.product_key, len(volume_df)
                            )
                        else:
                            volume_df = None
                    except Exception as exc:
                        logger.warning("ERP volume load failed for %s: %s", target.product_key, exc)
                        volume_df = None

                # Wave 3 T3.4 external-feed exogenous features (flag-gated)
                operating_rate_df = None
                inventory_df = None
                import_price_df = None
                if os.environ.get("FORECAST_EXTERNAL_EXOG_ENABLED", "false").lower() == "true":
                    parts = (target.product_key or "").split(".")
                    product_id = parts[1] if len(parts) >= 2 else (target.product_key or "")
                    try:
                        op_loader = OperatingRateLoader(
                            product_id=product_id, lookback_days=365,
                            org_id=target.org_id, db_session=self._db,
                        )
                        operating_rate_df = op_loader.load()
                        if not operating_rate_df.empty:
                            operating_rate_df = operating_rate_df.set_index("date")
                            logger.info(
                                "External op-rate loaded for %s: %d days",
                                target.product_key, len(operating_rate_df),
                            )
                        else:
                            operating_rate_df = None
                    except Exception as exc:
                        logger.warning(
                            "External op-rate load failed for %s: %s",
                            target.product_key, exc,
                        )
                        operating_rate_df = None

                    try:
                        inv_loader = InventoryLoader(
                            product_id=product_id, lookback_days=365,
                            org_id=target.org_id, db_session=self._db,
                        )
                        inventory_df = inv_loader.load()
                        if not inventory_df.empty:
                            inventory_df = inventory_df.set_index("date")
                            logger.info(
                                "External inventory loaded for %s: %d days",
                                target.product_key, len(inventory_df),
                            )
                        else:
                            inventory_df = None
                    except Exception as exc:
                        logger.warning(
                            "External inventory load failed for %s: %s",
                            target.product_key, exc,
                        )
                        inventory_df = None

                    try:
                        ip_loader = ImportPriceLoader(
                            product_id=product_id, lookback_days=365,
                            org_id=target.org_id, db_session=self._db,
                        )
                        import_price_df = ip_loader.load()
                        if not import_price_df.empty:
                            import_price_df = import_price_df.set_index("date")
                            logger.info(
                                "External import-price loaded for %s: %d days",
                                target.product_key, len(import_price_df),
                            )
                        else:
                            import_price_df = None
                    except Exception as exc:
                        logger.warning(
                            "External import-price load failed for %s: %s",
                            target.product_key, exc,
                        )
                        import_price_df = None

                # Build feature matrix for exog models (no cascade in single-target mode)
                feed_loader = FeedstockLoader()
                fx_loader = FxLoader()
                event_loader = EventFlagLoader(self._db)
                fm = build_features(
                    target.product_key, y_model, exog_spec,
                    feed_loader, fx_loader, event_loader, h_max, cascade_forecasts=None,
                    volume_df=volume_df,
                    operating_rate_df=operating_rate_df,
                    inventory_df=inventory_df,
                    import_price_df=import_price_df,
                )

                # Fit exog-aware models if feature matrix is available
                if fm.X_train is not None and len(fm.X_train) > 0:
                    features_used = list(fm.X_train.columns)
                    xgb_exog = models.get("xgboost_exog")
                    if xgb_exog is not None and hasattr(xgb_exog, "fit"):
                        try:
                            xgb_exog.fit(y_model, seasonal_period=sp, exog=fm.X_train)
                            exog_fc = xgb_exog.forecast(
                                h_max,
                                exog_future=fm.X_future if fm.X_future is not None and len(fm.X_future) > 0 else None,
                            )
                            forecasts["xgboost_exog"] = exog_fc
                            models_run.append("xgboost_exog")
                        except Exception as exc:
                            logger.warning("xgboost_exog fit failed for %s: %s", target.product_key, exc)
                            models_failed.append("xgboost_exog")
                            exog_degraded = True

                    # Foundation model: Moirai (exog-aware) — same exog as xgboost_exog
                    moirai_model = models.get("moirai")
                    if (moirai_model is not None and hasattr(moirai_model, "fit")
                            and getattr(moirai_model, "uses_exog", False)):
                        try:
                            moirai_model.fit(y_model, seasonal_period=sp, exog=fm.X_train)
                            moirai_fc = moirai_model.forecast(
                                h_max,
                                exog_future=fm.X_future if fm.X_future is not None and len(fm.X_future) > 0 else None,
                            )
                            forecasts["moirai"] = moirai_fc
                            models_run.append("moirai")
                        except Exception as exc:
                            logger.warning("moirai fit failed for %s: %s", target.product_key, exc)
                            models_failed.append("moirai")
            except Exception as exc:
                logger.warning("Exog pipeline failed for %s: %s", target.product_key, exc)
                exog_degraded = True

        # --- Ensemble (re-blend after exog models added) ---
        # Per-horizon weights: ARIMA may dominate at 7d while ETS/STL win at 30d.
        per_h = bt.per_horizon_mape if bt.per_horizon_mape else None
        ensemble_result = blend(
            forecasts, per_model_mape, per_model_error_by_horizon=per_h,
        )

        # --- Guard ---
        naive_fc = forecasts.get("seasonal_naive")
        if naive_fc is None:
            from app.services.forecasting.models.naive import SeasonalNaive
            sn = SeasonalNaive(seasonal_period=sp)
            try:
                sn.fit(y_model, seasonal_period=sp)
                naive_fc = sn.forecast(h_max)
            except Exception:
                naive_fc = ensemble_result.point_forecast

        guard_result = evaluate_guard(
            ensemble_result.point_forecast,
            naive_fc,
            bt.ensemble_mape,
            bt.naive_mape,
        )

        # --- Step 8: Coherence check (Phase 1) ---
        if exog_spec and exog_spec.feedstock_keys:
            for fk in exog_spec.feedstock_keys:
                if fk in forecasts:
                    fc_vals = ensemble_result.point_forecast.tolist()[:h_max]
                    coherence_report = check_coherence(
                        target.product_key, fc_vals,
                        forecasts[fk].tolist()[:h_max], fk,
                    )
                    if coherence_report.spread_inverted:
                        clamped, coherence_report = apply_coherence(
                            fc_vals, coherence_report,
                            forecasts[fk].tolist()[:h_max],
                        )
                        ensemble_result.point_forecast = pd.Series(
                            clamped, name=ensemble_result.point_forecast.name,
                        )
                    break   # check first feedstock only

        # --- Step 8.5: Intelligence overlay (Phase 3) ---
        intelligence_overlay_enabled = _get_config_bool(
            "FORECAST_INTELLIGENCE_OVERLAY_ENABLED", True
        )
        intelligence_report: dict | None = None
        if intelligence_overlay_enabled:
            try:
                from app.services.forecasting.intelligence_overlay import (
                    fetch_product_intelligence_sync,
                )
                intel = fetch_product_intelligence_sync(
                    self._db, target.product_key, target.org_id or "default-org"
                )
                intelligence_report = {
                    "net_price_pct": intel["net_price_pct"],
                    "confidence": intel["confidence"],
                    "event_count": intel["event_count"],
                    "bias": intel["bias"],
                    "horizon_adjustments": intel["horizon_adjustments"],
                }
                # Apply adjustment to the published forecast (7d = full reflection)
                if intel.get("active") and abs(intel["net_price_pct"]) > 0.01:
                    adj_pct = intel["horizon_adjustments"].get(7, 0.0)
                    factor = 1.0 + adj_pct / 100.0
                    gc = guard_result
                    gc.published_forecast = gc.published_forecast * factor
                    # Log top event
                    top = intel["detail"][0] if intel["detail"] else None
                    logger.info(
                        "[intel-overlay] %s: %.2f%% (×%.4f, %d events, bias=%s)%s",
                        target.product_key,
                        adj_pct,
                        factor,
                        intel["event_count"],
                        intel["bias"],
                        f" — {top['headline'][:80]}" if top else "",
                    )
            except Exception as exc:
                logger.warning(
                    "[intel-overlay] failed for %s: %s", target.product_key, exc
                )
                intelligence_report = None

        # --- Step 8.55: Domain signals overlay (Phase B) ---
        domain_signals_enabled = _get_config_bool(
            "FORECAST_DOMAIN_SIGNALS_ENABLED", False
        )
        domain_signals_report: dict | None = None
        if domain_signals_enabled:
            try:
                from app.services.forecasting.domain_signals import (
                    compute_domain_signal_adjustment,
                    fetch_naphtha_recent_pct_change,
                )
                # Extract product_id from target.product_key.
                # For family targets: "ecisco.c5_resin" → "c5_resin"
                # For SKU targets:   "ecisco.c5_resin.201000018" → "c5_resin"
                parts = (target.product_key or "").split(".")
                product_id = parts[1] if len(parts) >= 2 else (target.product_key or "")

                naphtha_pct = fetch_naphtha_recent_pct_change(
                    self._db, org_id=target.org_id or "default-org"
                )

                domain_signals_report = compute_domain_signal_adjustment(
                    product_id=product_id,
                    as_of_date=datetime.now(),
                    naphtha_pct_change=naphtha_pct,
                )

                # Apply the combined % adjustment to the published forecast
                # (same pattern as the intelligence overlay in Step 8.5).
                total_pct = domain_signals_report.get("total_pct", 0.0)
                if total_pct != 0.0:
                    guard_result.published_forecast = (
                        guard_result.published_forecast * (1.0 + total_pct / 100.0)
                    )

                logger.info(
                    "[domain-signals] %s: applied %+.4f%% (seasonal=%+.4f, causal=%+.4f, rules=%s)",
                    target.product_key, total_pct,
                    domain_signals_report.get("seasonal_pct", 0.0),
                    domain_signals_report.get("causal_pct", 0.0),
                    domain_signals_report.get("applied_rules", []),
                )
            except Exception as exc:
                logger.warning(
                    "[domain-signals] failed for %s: %s", target.product_key, exc
                )
                domain_signals_report = None

        # --- Step 8.6: Policy service — bias correction + volatility adjustment (Phase 4) ---
        policy_enabled = _get_config_bool("FORECAST_POLICY_ENABLED", True)
        policy_report: dict | None = None
        if policy_enabled:
            try:
                from app.services.forecasting.forecast_policy_service import (
                    ForecastPolicyService,
                )
                import numpy as np
                daily_returns = (
                    np.log(y / y.shift(1)).dropna().tolist()
                    if len(y) > 1 else []
                )
                policy_metrics = ForecastPolicyService.compute_from_accuracy_log(
                    db=self._db,
                    product_key=target.product_key,
                    org_id=target.org_id or "default-org",
                    daily_returns=daily_returns,
                )
                total_adj = policy_metrics.bias_pct + policy_metrics.diagnosis_bias
                factor = (1.0 + total_adj / 100.0) * policy_metrics.vol_multiplier
                guard_result.published_forecast = (
                    guard_result.published_forecast * factor
                )
                policy_report = {
                    "bias_pct": policy_metrics.bias_pct,
                    "diagnosis_bias": policy_metrics.diagnosis_bias,
                    "volatility_regime": policy_metrics.volatility_regime.value,
                    "vol_multiplier": policy_metrics.vol_multiplier,
                    "sample_count": policy_metrics.sample_count,
                    "mean_signed_error": policy_metrics.mean_signed_error,
                    "daily_vol_std": policy_metrics.daily_vol_std,
                    "applied_factor": round(factor, 4),
                }
                logger.info(
                    "[policy] %s: bias=%.3f%%, vol=%s(mult=%.2f), factor=×%.4f, n=%d",
                    target.product_key,
                    policy_metrics.bias_pct,
                    policy_metrics.volatility_regime.value,
                    policy_metrics.vol_multiplier,
                    factor,
                    policy_metrics.sample_count,
                )
            except Exception as exc:
                logger.warning(
                    "[policy] failed for %s: %s", target.product_key, exc
                )
                policy_report = None

        # --- Step 9: Explanation + trust + directional + decision ---
        # Built AFTER probability_report (Phase D) so directional+decision
        # can consume probability values. Previously this block was built
        # BEFORE probability_report was defined, raising NameError and
        # silently leaving run.explanation = None — which broke Phase 6/7
        # overlay-meta badges. Now assembled in correct order.
        drift_status = detect_drift(self._db, target.product_key)
        explanation_dict: dict | None = None
        xgb_exog = forecasts.get("xgboost_exog")

        # ── MLOps / HITL publish-step adjustments (Task 7) ──────────────
        # Both adjust guard_result.published_forecast (the price users see)
        # but NEVER the decision-engine action call. Disclosed in the brief.
        # Flags default OFF -> no-op, existing behavior unchanged.
        drift_report: dict | None = None
        if _get_config_bool("FORECAST_DRIFT_AUTO_ADJUST_ENABLED", False):
            try:
                from app.models.forecasting import ForecastWeightAdjustment
                from app.services.forecasting.ops import drift_response
                pending_drift = self._db.query(ForecastWeightAdjustment).filter(
                    ForecastWeightAdjustment.target_id == target_id,
                    ForecastWeightAdjustment.triggered_by == "drift",
                    ForecastWeightAdjustment.applied == False,  # noqa: E712
                ).order_by(ForecastWeightAdjustment.created_date.desc()).first()
                if pending_drift is not None and naive_fc is not None:
                    f = drift_response.get_drift_blend_factor()
                    blended = guard_result.published_forecast * (1 - f) + naive_fc * f
                    guard_result = dataclasses.replace(
                        guard_result, published_forecast=blended,
                    )
                    pending_drift.applied = True
                    pending_drift.applied_at = datetime.utcnow()
                    self._db.flush()
                    drift_report = {
                        "blend_factor": f,
                        "audit_id": pending_drift.id,
                        "reason": pending_drift.reason,
                    }
                    logger.info("[ops] drift-blend applied to %s (f=%.2f)", target.product_key, f)
            except Exception:
                logger.exception("[ops] drift-blend failed for %s", target.product_key)

        bias_report: dict | None = None
        if _get_config_bool("FORECAST_BIAS_CORRECTION_ENABLED", False):
            try:
                from app.services.forecasting.ops import bias_correction
                _author = bias_correction.resolve_trusted_author(self._db, target.product_key)
                _adjusted, bias_report = bias_correction.apply_bias_correction(
                    self._db, target, guard_result.published_forecast,
                    author_id=_author,
                )
                if bias_report is not None:
                    guard_result = dataclasses.replace(
                        guard_result, published_forecast=_adjusted,
                    )
            except Exception:
                logger.exception("[ops] bias-correction failed for %s", target.product_key)

        # --- Scenarios (with conformal calibrated intervals) ---
        cal = conformal_calibrate(bt.residuals_by_horizon) if bt.residuals_by_horizon else None
        scenarios_result = generate(
            guard_result.published_forecast,
            residuals=bt.residuals,
            mape=bt.ensemble_mape,
            horizons=horizons,
            calibration=cal,
        )

        # --- Price-change probability (Phase D) ---
        last_actual = float(y_model.iloc[-1]) if len(y_model) > 0 else None
        probability_report: dict = {}
        if cal is not None and last_actual:
            try:
                for h in horizons:
                    pcp = compute_price_change_probability(
                        guard_result.published_forecast,
                        last_actual=last_actual,
                        calibration=cal,
                        horizon=h,
                    )
                    probability_report[str(h)] = {
                        "p_rise": round(pcp.p_rise, 3),
                        "p_rise_gt": {str(k): round(v, 3) for k, v in pcp.p_rise_gt.items()},
                        "expected_change_pct": round(pcp.expected_change_pct, 4),
                    }
            except Exception as exc:
                logger.warning("Price-change probability failed for %s: %s", target.product_key, exc)

        # --- Phase E1: directional classifier (per horizon) ---
        from app.services.forecasting.directional_classifier import backtest_directional
        from app.services.forecasting.decision_engine import recommend

        directional_report: dict = {}
        decision_report: dict = {}
        for h in horizons:
            h_int = int(h)
            try:
                dir_result = backtest_directional(y_model, horizons=(h_int,))
            except Exception as exc:
                logger.warning(
                    "[directional] failed for %s h=%d: %s",
                    target.product_key, h_int, exc,
                )
                dir_result = {"status": "no_edge"}

            acc = dir_result.get("logistic")
            directional_report[str(h)] = {
                "accuracy": acc,
                "n_test": int(dir_result.get("n_test", 0) or 0),
                "p_value": dir_result.get("p_value"),
                "status": dir_result.get("status", "no_edge"),
            }

            # Decision needs trust_tier which is built below; placeholder for now
            decision_report[str(h)] = None

        # --- Step 9: Explanation + trust tier + decisions (Phase E) ---
        try:
            explanation = explain_forecast(
                product_key=target.product_key,
                forecast_values=ensemble_result.point_forecast.tolist()[:h_max],
                previous_forecast=None,  # No previous comparison in single mode
                xgboost_model=xgb_exog,
                feature_names=features_used,
                cleaning_report=cleaning_report if cleaning_report else None,
                coherence_report=coherence_report if coherence_report else None,
                drift_status=drift_status,
                honesty_gate_triggered=guard_result.below_naive_baseline,
            )

            # --- Trust tier (Phase B) ---
            trust_tier_report: dict | None = None
            try:
                from app.services.forecasting.forecast_trust_tier import (
                    classify_cadence,
                    compute_forecast_trust_tier,
                )
                _parts = (target.product_key or "").split(".")
                _product_id = _parts[1] if len(_parts) >= 2 else (target.product_key or "")
                _cadence = classify_cadence(_product_id, row_count=None)
                trust_tier_report = compute_forecast_trust_tier(
                    product_id=_product_id,
                    below_naive=guard_result.below_naive_baseline,
                    cadence_class=_cadence,
                    mape=None,
                )
            except Exception as exc:
                logger.warning("[trust-tier] failed for %s: %s", target.product_key, exc)

            # --- Phase E2: decision engine (per horizon) ---
            # Now we have trust_tier, fill in decisions
            tier = (trust_tier_report or {}).get("tier") or "low"
            for h_key in decision_report.keys():
                prob_h = probability_report.get(h_key) or {}
                p_rise = float(prob_h.get("p_rise", 0.5) or 0.5)
                exp_chg = prob_h.get("expected_change_pct")
                exp_chg_f = float(exp_chg) if exp_chg is not None else 0.0
                dir_h = directional_report.get(h_key) or {}
                decision = recommend(
                    p_rise=p_rise,
                    expected_change_pct=exp_chg_f,
                    directional_acc=dir_h.get("accuracy"),
                    directional_status=dir_h.get("status"),
                    trust_tier=str(tier),
                )
                decision_report[h_key] = {
                    "action": decision.action,
                    "confidence": decision.confidence,
                    "rationale": decision.rationale,
                }

            # --- Model agreement metric (analyst evidence) ---
            from app.services.forecasting.analyst.evidence_pack import (
                compute_model_agreement,
            )
            model_agreement_report: dict = {}
            for h in horizons:
                h_int = int(h)
                ag = compute_model_agreement(forecasts, h_int)
                if ag is not None:
                    model_agreement_report[str(h_int)] = ag

            explanation_dict = {
                "summary": explanation.summary,
                "confidence": explanation.confidence,
                "drivers": [{"feature": d.feature, "weight": d.weight} for d in explanation.drivers],
                "coherence_flags": explanation.coherence_flags,
                "drift_warning": explanation.drift_warning,
                "intelligence": intelligence_report,
                "policy": policy_report,
                "domain_signals": domain_signals_report,
                "trust_tier": trust_tier_report,
                "probability": probability_report,
                "directional": directional_report,
                "decision": decision_report,
                "model_agreement": model_agreement_report,
            }
        except Exception as exc:
            logger.warning("Explanation failed for %s: %s", target.product_key, exc)

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
            "ensemble_mape": bt.ensemble_mape if math.isfinite(bt.ensemble_mape) else None,
            "naive_mape": bt.naive_mape if math.isfinite(bt.naive_mape) else None,
            "metric": bt.metric,
        }

        # --- Write ForecastRun ---
        now_utc = datetime.now(timezone.utc)
        # merge MLOps/HITL adjustment reports into the run explanation
        ops_explanation: dict = {}
        if drift_report is not None:
            ops_explanation["drift_adjustment"] = drift_report
        if bias_report is not None:
            ops_explanation["bias_correction"] = bias_report
        if ops_explanation:
            explanation_dict = {**(explanation_dict or {}), **ops_explanation}
        run = ForecastRun(
            target_id=target_id,
            org_id=target.org_id,
            app_id=target.app_id,
            results=results,
            below_naive_baseline=guard_result.below_naive_baseline,
            confidence=scenarios_result.confidence,
            as_of_date=as_of if as_of is not None else now_utc,
            created_date=as_of if as_of is not None else now_utc,
            model_detail=model_detail,
            exog_features_used=features_used if features_used else None,
            cleaning_notes=cleaning_report.notes if cleaning_report else None,
            explanation=explanation_dict,
            cleaning_report=(
                {
                    "n_spikes": cleaning_report.n_spikes_detected,
                    "spike_dates": cleaning_report.spike_dates,
                    "n_shifts": cleaning_report.n_level_shifts,
                    "is_stale": cleaning_report.is_stale,
                    "winsorized": cleaning_report.winsorization_applied,
                }
                if cleaning_report else None
            ),
            coherence_report=(
                {
                    "violations": coherence_report.violations,
                    "spread_inverted": coherence_report.spread_inverted,
                    "clamped": coherence_report.clamped,
                }
                if coherence_report else None
            ),
            exog_degraded=exog_degraded,
        )
        self._db.add(run)

        # ------------------------------------------------------------------
        # T2.1: Flag-gated decision logging (FORECAST_DECISION_LOGGING_ENABLED)
        # Log each horizon's decision with actual_price_t so the decision→ROI
        # loop can close during the nightly scoring cron.
        # ------------------------------------------------------------------
        if _get_config_bool("FORECAST_DECISION_LOGGING_ENABLED", False):
            try:
                from app.services.forecasting.features.decision_logger import (
                    log_decision,
                )
                # Last known price at decision time — the critical field
                # that makes get_pending_unrealized() return this log.
                if y_model is not None and len(y_model) > 0:
                    actual_price_t = float(y_model.iloc[-1])
                else:
                    actual_price_t = None

                # Per-horizon thresholds (the values used for THIS run)
                from app.services.forecasting import decision_engine as _de
                decision_thresholds = {
                    "buy": _de._BUY_THRESHOLD,
                    "sell": _de._SELL_THRESHOLD,
                    "buy_min_change": _de._BUY_MIN_CHANGE,
                    "edge": _de._EDGE_THRESHOLD,
                }

                for h in horizons:
                    key = str(int(h))
                    decision_entry = decision_report.get(key)
                    probability_entry = probability_report.get(key)
                    if decision_entry is None:
                        continue

                    p_rise = probability_entry.get("p_rise") if probability_entry else None
                    exp_chg = probability_entry.get("expected_change_pct") if probability_entry else None

                    log_decision(
                        session=self._db,
                        product_id=target.product_key,
                        horizon_day=int(h),
                        as_of_date=as_of,
                        action=str(decision_entry.action),
                        confidence=(decision_entry.confidence or "low"),
                        rationale=(decision_entry.rationale or ""),
                        forecast_run_id=run.id,
                        predicted_p_rise=float(p_rise) if p_rise is not None else None,
                        predicted_change_pct=float(exp_chg) if exp_chg is not None else None,
                        decision_thresholds=decision_thresholds,
                        actual_price_t=actual_price_t,
                    )
            except Exception:
                logger.exception(
                    "[forecast-engine] decision logging failed for %s",
                    target.product_key,
                )

        # --- Write ForecastAccuracyLog ---
        # Sanitize per_model_mape: replace non-finite values (Infinity, NaN)
        # with None so the JSON column doesn't reject them. XGBoost can
        # produce Infinity MAPE on degenerate series (e.g. all-zero targets).
        per_model_safe = {
            name: (None if (m is None or not math.isfinite(m)) else m)
            for name, m in per_model_mape.items()
        }
        for name, mape in per_model_mape.items():
            for h in horizons:
                log_entry = ForecastAccuracyLog(
                    target_id=target_id,
                    org_id=target.org_id,
                    app_id=target.app_id,
                    horizon_days=h,
                    mape=mape if (mape is not None and math.isfinite(mape)) else None,
                    naive_mape=bt.naive_mape
                    if (bt.naive_mape is not None and math.isfinite(bt.naive_mape))
                    else None,
                    skill_vs_naive=(
                        (mape - bt.naive_mape)
                        if (mape is not None and math.isfinite(mape)
                            and bt.naive_mape is not None and math.isfinite(bt.naive_mape))
                        else None
                    ),
                    below_naive_baseline=guard_result.below_naive_baseline,
                    per_model=per_model_safe,
                )
                self._db.add(log_entry)

        self._db.flush()

        # --- Apply business rules ---
        # (Future: seasonal adjustments, event overrides, guardrails)
        _apply_rules(self._db, target_id, run)

        _prewarm_analyst_brief(self._db, target.product_key)

        return run

    # ── Nightly batch ──────────────────────────────────────────────

    def compute_all(
        self,
        org_id: str,
        horizons: list[int] | None = None,
        seasonal_period: int = _DEFAULT_SEASONAL_PERIOD,
    ) -> list[ForecastRun]:
        """Compute forecasts for all active targets in an org.

        When FORECAST_CASCADE_ENABLED=True, orders targets topologically
        (feedstock → derivative) and populates cascade_forecasts dict
        so downstream products can use upstream forecasts as exogenous
        features.
        """
        targets = (
            self._db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == org_id,
                ForecastTarget.status.in_(["active", "discovered"]),
                ForecastTarget.is_deleted == False,
            )
            .all()
        )

        cascade_enabled = _get_config_bool("FORECAST_CASCADE_ENABLED", True)
        cascade_forecasts: dict[str, list[float]] = {}

        if cascade_enabled and len(targets) > 0:
            try:
                from app.services.knowledge_graph.graph import build_c5_c9_graph
                kg = build_c5_c9_graph()
                product_keys = [t.product_key for t in targets]
                ordered_keys = topological_order(product_keys, kg)
                # Sort targets by topological order
                key_index = {k: i for i, k in enumerate(ordered_keys)}
                targets = sorted(
                    targets,
                    key=lambda t: key_index.get(t.product_key, 999),
                )
            except Exception as exc:
                logger.warning("Topological ordering failed: %s — using original order", exc)

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
                    # Populate cascade_forecasts for downstream products
                    if cascade_enabled and run.results:
                        max_h = max(int(k) for k in run.results.keys()) if run.results else 7
                        base_fc = run.results.get(str(max_h), {}).get("base", [])
                        if base_fc:
                            cascade_forecasts[target.product_key] = list(base_fc)
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
        as_of: datetime | None = None,
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
            run = self.compute_target(target_id, horizons, seasonal_period, as_of=as_of)
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

def _prewarm_analyst_brief(db, product_key) -> None:
    """Best-effort analyst brief pre-warm after a forecast run. Never raises."""
    try:
        if not _get_config_bool("FORECAST_ANALYST_LLM_ENABLED", False):
            return
        parts = (product_key or "").split(".")
        if len(parts) != 2 or parts[0] != "ecisco":
            return
        from app.services.forecasting.analyst import service as analyst_service
        analyst_service.prewarm_brief(parts[1], day=7, db=db)
    except Exception as exc:
        logger.warning("[analyst] prewarm failed for %s: %s", product_key, exc)


def _get_config_bool(key: str, default: bool = True) -> bool:
    """Safely read a boolean config flag, falling back to *default*."""
    try:
        from app.config import settings
        return bool(getattr(settings, key, default))
    except Exception:
        return default


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
