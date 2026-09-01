"""Forecasting domain models — 5 tables for the enterprise BI forecasting engine.

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
naive seasonal baseline (a hard-won lesson from the reference deployment).
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
    fixing the gap of logging only aggregate MAPE.
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
    mae: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rmse: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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

    # ── Realized-eval linkage (MLOps) ─────────────────────────
    # Links a realized-eval row (per_model=None marker) back to its ForecastRun,
    # so drift detection / adaptive weights consume only scored rows.
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)


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
    #   causal_driver:   {driver: "feedstock", elasticity: 0.82}
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


class ForecastFeedback(TimestampedBase):
    """Human-in-the-Loop correction — a user asserts the AI price is wrong.

    ``status`` flows ``pending`` → ``scored`` once the target date's actual
    arrives and the evaluation job computes ``ai_error`` / ``user_error`` /
    ``beat``. A pending override NEVER moves the forecast; only trust-gated,
    validated corrections influence the published price (bias_correction.py).
    """

    __tablename__ = "forecast_feedback"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ai_price: Mapped[float] = mapped_column(Float, nullable=False)
    user_price: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    author_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    ai_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    user_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    beat: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ForecastWeightAdjustment(TimestampedBase):
    """Audit trail for automated forecast adjustments (drift, bias-correction).

    Every automated change to a published forecast writes a row here so the
    change is traceable. ``applied=False`` on creation; the engine marks it
    ``True`` once the adjustment is blended into a run.
    """

    __tablename__ = "forecast_weight_adjustments"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)  # drift | bias_correction
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    old_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    new_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    delta_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ForecastDecisionLog(TimestampedBase):
    """Logs every forecast decision with inputs + realized outcome for ROI measurement.

    Created once per (forecast_run, product, horizon_day) when a decision is made.
    Realized outcomes are filled later by the accuracy_tracker when actual prices
    become available (horizon_day days after as_of_date).

    Enables Wave 1 threshold calibration (T1.4 grid-search over decision thresholds)
    by replaying decisions under different threshold values.
    """
    __tablename__ = "forecast_decision_logs"

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    # NOTE (2026-08-05): was incorrectly declared ``Integer`` here, which
    # broke ``Base.metadata.create_all()`` on every backend startup with
    # ``foreign key constraint "forecast_decision_logs_forecast_run_id_fkey"
    # cannot be implemented / Key columns "forecast_run_id" and "id" are
    # of incompatible types: integer and character varying``. The
    # referenced ``forecast_runs.id`` is a UUID String(36) from
    # ``TimestampedBase``, so the FK must be a String(36) too. The column
    # is still optional (``nullable=True``) because some decision logs
    # originate from rules / domain signals without a backing run.
    forecast_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("forecast_runs.id"), nullable=True, index=True,
    )
    product_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # ------------------------------------------------------------------ #
    # Snapshot: when and for what horizon
    # ------------------------------------------------------------------ #
    horizon_day: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Days ahead this decision was made for (e.g. 30)",
    )
    as_of_date: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        comment="The date the decision was computed",
    )

    # ------------------------------------------------------------------ #
    # Inputs that produced the decision (for threshold calibration replay)
    # ------------------------------------------------------------------ #
    predicted_p_rise: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    predicted_change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_thresholds: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Snapshot of thresholds: {buy_p, sell_p, min_change, edge_accuracy}",
    )

    # ------------------------------------------------------------------ #
    # Decision output
    # ------------------------------------------------------------------ #
    action: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="buy / sell / hold / watch",
    )
    confidence: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
        comment="high / medium / low",
    )
    rationale: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # ------------------------------------------------------------------ #
    # Realized outcomes (filled later)
    # ------------------------------------------------------------------ #
    actual_price_t: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Price at decision time (as_of_date)",
    )
    actual_price_th: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Price at as_of_date + horizon_day",
    )
    roi_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="(actual_price_th/actual_price_t - 1)*100, signed for buy/sell",
    )
    realized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
        comment="When the realized outcome was filled",
    )


class ChallengerShadowRun(TimestampedBase):
    """Champion/challenger shadow-run results — persists nightly shadow metrics.

    One row per (target_id, challenger_type, run_date).  The nightly
    champion/challenger step writes these after each shadow run.  Auto-promotion
    reads consecutive weeks of shadow runs to decide if a challenger should
    replace the champion.
    """
    __tablename__ = "challenger_shadow_runs"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True,
    )
    product_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    challenger_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="e.g. 'stacking_meta', 'xgboost_tuned', 'var'",
    )
    challenger_config: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Model config used for this challenger run",
    )

    # ── Metrics ───────────────────────────────────────────────
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    shadow_mape: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    champion_mape: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shadow_delta_mape: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="champion_mape - shadow_mape (positive = challenger wins)",
    )

    # ── Lifecycle ─────────────────────────────────────────────
    run_date: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        comment="Date of the nightly run",
    )
    promoted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True once auto-promotion has applied this challenger",
    )


class ForecastEventImpact(TimestampedBase):
    """Quantified impact of exogenous events on forecast accuracy (Wave 2 T2.2).

    Stores per-event calibration data: how much did a specific event type
    shift the forecast error? Feeds the evidence-pack demand signal and
    the brief-writer's event-weighting logic in Wave 1-2.
    """
    __tablename__ = "forecast_event_impacts"

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    product_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # ------------------------------------------------------------------ #
    # Event descriptor
    # ------------------------------------------------------------------ #
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="market / maintenance / regulatory / logistics / demand_spike / supplier_outage",
    )
    event_date: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        comment="Date the event occurred (or started)",
    )
    event_label: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True,
        comment="Human-readable description",
    )

    # ------------------------------------------------------------------ #
    # Impact quantification
    # ------------------------------------------------------------------ #
    price_impact_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Observed price % change attributable to this event",
    )
    volume_impact_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Observed volume % change attributable to this event",
    )
    forecast_error_shift: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Change in MAPE during the event window vs baseline",
    )
    duration_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="How many days the impact persisted",
    )

    # ------------------------------------------------------------------ #
    # Source and confidence
    # ------------------------------------------------------------------ #
    source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
        comment="Data source: erp_volume / supplier_ladder / intel_agent / manual",
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="0.0-1.0 calibration confidence",
    )


# ---------------------------------------------------------------------------
# Wave 2 T2.4: DB-backed per-product decision threshold config
# ---------------------------------------------------------------------------

class ForecastThresholdConfig(TimestampedBase):
    """Per-product (or global) decision threshold configuration.

    Resolved at runtime by ``decision_engine.get_thresholds(product_key)``:
    active product-specific → active global → env → hardcoded default.

    Thresholds are staged by the auto-tuner (source="autotune", status="staged")
    and promoted to active by an admin via POST /forecast-ops/apply-thresholds.
    """

    __tablename__ = "forecast_threshold_config"

    product_key: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True,
        comment="NULL = global default override",
    )
    buy_threshold: Mapped[float] = mapped_column(Float, nullable=False,
                                                  default=0.70)
    sell_threshold: Mapped[float] = mapped_column(Float, nullable=False,
                                                   default=0.30)
    buy_min_change: Mapped[float] = mapped_column(Float, nullable=False,
                                                   default=0.03)
    sell_min_change: Mapped[float] = mapped_column(Float, nullable=False,
                                                    default=-0.03)
    edge_threshold: Mapped[float] = mapped_column(Float, nullable=False,
                                                   default=0.55)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual",
        comment="manual | autotune | autotune (applied)",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="staged",
        comment="staged | active",
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
        comment="When the config was promoted to active",
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="Who created this config",
    )


# ---------------------------------------------------------------------------
# Wave 3 T3.0: External-feed store (CSV-onboarded time series)
# ---------------------------------------------------------------------------

# Allowed domain values for ForecastExternalSeries.domain. The column is a
# free string at the DB level so new domains can be added without a migration;
# this constant is the canonical list for the Wave 3 default domains.
EXTERNAL_FEED_DOMAINS: tuple[str, ...] = (
    "operating_rate",  # 下游开工率
    "inventory",       # 港口/社会库存
    "import_price",    # 进口/竞争对手价格
)

# Allowed source values for ForecastExternalSeries.source.
EXTERNAL_FEED_SOURCES: tuple[str, ...] = (
    "csv_upload",  # user-uploaded CSV via /external-feeds endpoint
    "api",         # automated API ingestion adapter (future)
    "mock",        # mock data generator (development only)
)


class ForecastExternalSeries(TimestampedBase):
    """Registry of externally-onboarded time-series feeds (Wave 3 T3.0).

    One row per onboarded feed (e.g. ``op_rate_isoprene``). Loaders query
    ``forecast_external_points`` filtered by ``domain`` (and optionally
    ``product_key``) to pull the raw values needed for XGBoost exogenous
    features and AI Brief signals.

    Populated by:
      - CSV upload via ``POST /api/forecast-ops/external-feeds``
      - Mock data generator via ``POST /api/forecast-ops/external-feeds/seed-mock``
      - (Future) automated API ingestion adapters
    """

    __tablename__ = "forecast_external_series"
    __table_args__ = (
        UniqueConstraint("series_key", "org_id", name="uq_ext_series_key_org"),
    )

    # ── Identity ──────────────────────────────────────────────
    series_key: Mapped[str] = mapped_column(
        String(150), nullable=False, index=True,
        comment="Unique feed identifier within org, e.g. 'op_rate_isoprene'",
    )
    domain: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="Feed type: operating_rate | inventory | import_price (free string for forward-compat)",
    )
    product_key: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True,
        comment="Forecast product this feed applies to (NULL = industry-wide)",
    )

    # ── Metadata ──────────────────────────────────────────────
    unit: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True,
        comment="Unit string, e.g. '%', '吨', 'CNY/kg'",
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="csv_upload",
        comment="csv_upload | api | mock",
    )
    cadence: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
        comment="daily | weekly | monthly | irregular",
    )

    # ── Roll-up stats (maintained by ingest endpoints) ────────
    last_value_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
        comment="Date of the most recent point in this series",
    )
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Cached count of points in forecast_external_points",
    )

    # ── Provenance ────────────────────────────────────────────
    uploaded_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="User ID or system component that created this series",
    )
    notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Free-form notes about this feed",
    )


class ForecastExternalPoint(TimestampedBase):
    """Individual data point for an external series (Wave 3 T3.0).

    Loaders query by ``series_id`` with a date range filter to assemble
    a DataFrame (``['date', '<metric>']``) that mirrors the shape of
    ``ErpVolumeLoader`` / ``SupplierDispersionLoader``.

    Uniqueness is enforced on ``(series_id, date)`` so uploads of the same
    date replace (upsert) rather than duplicate.
    """

    __tablename__ = "forecast_external_points"
    __table_args__ = (
        UniqueConstraint("series_id", "date", name="uq_ext_point_series_date"),
    )

    series_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("forecast_external_series.id"),
        nullable=False, index=True,
        comment="FK to forecast_external_series.id",
    )
    date: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        comment="Observation date (no time component expected for daily/weekly feeds)",
    )
    value: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Numeric observation value",
    )
    # NOTE: column is named ``metadata`` in DB but Python attr is ``metadata_``
    # to avoid colliding with SQLAlchemy's ``MetaData`` class attribute
    # (the same pattern used by AgentConversation, see base.py:to_dict comment).
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSON, nullable=True,
        comment="Optional provenance / region / source-row tags",
    )

