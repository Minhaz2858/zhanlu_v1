"""Forecasting domain models — 5 tables for the Ecisco BI Agent forecasting engine.

Tables:
    forecast_targets          — registry of forecastable series (any product/schema)
    forecast_runs             — cached forecast results (3d/7d/30d × base/bull/bear)
    forecast_accuracy_log     — nightly backtest records with per-model MAPE
    forecast_business_rules    — seasonal / causal / event / guardrail rules
    domain_pack_installs      — domain-pack version tracking

All tables inherit ``TimestampedBase`` (UUID PK, org_id/app_id tenant wall,
soft-delete, timestamps).  The ``below_naive_baseline`` boolean on
``forecast_runs`` and ``forecast_accuracy_log`` is the schema-level honesty
gate — it prevents silently shipping forecasts that fail to outperform a
naive seasonal baseline (EDIA's hard-won lesson).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Integer, Float, Boolean, JSON, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ForecastTarget(TimestampedBase):
    """Registry of forecastable series — one row per (product, datasource mapping).

    Created by the discovery algorithm (Section 2.2) or seeded from a domain
    pack (Section 3.7).  ``product_key`` is unique within an org so the same
    product can't be registered twice.
    """

    __tablename__ = "forecast_targets"
    __table_args__ = (
        UniqueConstraint("product_key", "org_id", name="uq_forecast_targets_product_key_org_id"),
    )

    # ── Identity ──────────────────────────────────────────────
    product_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── Datasource mapping (hints, not hard requirements) ─────
    # JSON: {table, time_col, measure, dims, granularity, region, ...}
    # Discovery fills this from schema scan; pack install fills from source_hints.
    datasource: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Autonomy level ───────────────────────────────────────
    # 0 = baseline only, 1 = statistical, 2 = pack-grade, 3 = pack + rules
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Quality ───────────────────────────────────────────────
    quality_grade: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    quality_stats: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="discovered", index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="discovery")

    # ── Model configuration ──────────────────────────────────
    # JSON: {arima_order, seasonal_period, model_tier, ...}
    model_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Weekly report ─────────────────────────────────────────
    include_in_weekly_report: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    report_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ForecastRun(TimestampedBase):
    """Cached forecast result for a target — written by the nightly compute job.

    ``results`` JSON structure::

        {
          "3d":  {"base": 1240, "bull": 1380, "bear": 1100},
          "7d":  {"base": 8650, "bull": 9400, "bear": 7900},
          "30d": {"base": 37200, "bull": 41000, "bear": 33400}
        }

    ``below_naive_baseline`` is the honesty gate: when ``true``, the ensemble
    failed to beat a seasonal-naive baseline and the published forecast falls
    back to the naive value with a warning.
    """

    __tablename__ = "forecast_runs"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )

    # ── Forecast payload ─────────────────────────────────────
    results: Mapped[dict] = mapped_column(JSON, nullable=False)

    # ── Honesty gate (schema-level, not a log line) ──────────
    below_naive_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ── Confidence & provenance ──────────────────────────────
    confidence: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    as_of_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # JSON: {models_run: [...], weights: {ets: 0.4, arima: 0.3, ...}, failed: [...]}
    model_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Exogenous features (Phase 1 enhancement) ──────────────
    exog_features_used: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    cleaning_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Explanation + coherence (Phase 1 enhancement) ──────────
    explanation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cleaning_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    coherence_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    exog_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ForecastAccuracyLog(TimestampedBase):
    """Nightly backtest record — per-model MAPE and naive comparison.

    Written by the backtest job (Section 2.5) after each compute cycle.
    ``per_model`` JSON captures every model's MAPE for audit + re-tuning,
    fixing EDIA's gap of logging only aggregate MAPE.
    """

    __tablename__ = "forecast_accuracy_log"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )

    # ── Backtest scope ───────────────────────────────────────
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    n_backtests: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    window_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    window_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Realized accuracy (Phase 1 enhancement) ────────────────
    realized_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_mape: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    # ── Accuracy metrics ──────────────────────────────────────
    mape: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    naive_mape: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # skill_vs_naive = mape - naive_mape; negative means ensemble beat naive
    skill_vs_naive: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Honesty gate ─────────────────────────────────────────
    below_naive_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # JSON: {"ets": 0.12, "arima": 0.15, "seasonal_naive": 0.14, "xgboost": 0.11}
    per_model: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class ForecastBusinessRule(TimestampedBase):
    """Business logic layer — seasonal, causal, event, and guardrail rules.

    Rules follow a ``proposed → active → archived`` workflow.  Rules from a
    domain pack are seeded as ``active``; rules captured from chat start as
    ``proposed`` and require explicit approval before affecting forecasts.
    ``target_id`` is nullable for global guardrails that apply to all targets.
    """

    __tablename__ = "forecast_business_rules"

    target_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=True, index=True
    )

    # seasonal / causal_driver / event_override / guardrail
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # JSON params are rule-type specific:
    #   seasonal:        {month: 11, adjustment_pct: -2.5}
    #   causal_driver:   {driver: "naphtha", elasticity: 0.82}
    #   event_override:  {event: "post_holiday_lull", month: 2, adjustment_pct: 0.8}
    #   guardrail:       {min_history: 14, max_mape: 0.3}
    params: Mapped[dict] = mapped_column(JSON, nullable=False)

    # proposed / active / paused / archived / rejected
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="proposed", index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="chat")

    # ── Approval workflow ────────────────────────────────────
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    approved_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DomainPackInstall(TimestampedBase):
    """Domain-pack install record — tracks which pack version is active.

    One row per (pack_key, org_id, app_id).  ``config`` stores the installed
    pack JSON snapshot for audit and re-install.
    """

    __tablename__ = "domain_pack_installs"

    pack_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pack_version: Mapped[str] = mapped_column(String(50), nullable=False)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
