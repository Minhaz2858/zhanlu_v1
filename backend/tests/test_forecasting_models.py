"""Unit tests for the forecasting domain models (Section 1).

Validates:
    - All 5 models inherit TimestampedBase
    - Table names match the migration
    - Required columns exist
    - Default values are correct (esp. below_naive_baseline = False — the honesty gate)
    - FK relationships to forecast_targets.id
    - JSON columns accept dict/list payloads
    - CRUD round-trips through SQLite
    - The proposed → active rule workflow
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_forecasting.db")

import pytest
from app.database import Base, engine, SessionLocal
import app.models  # noqa — registers all models for metadata

# Create all tables in the test SQLite DB
Base.metadata.create_all(engine)

from app.models.forecasting import (
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
    ForecastBusinessRule,
    DomainPackInstall,
)
from app.models.base import TimestampedBase


@pytest.fixture(autouse=True)
def _clean_forecast_tables():
    """Delete all rows from forecast tables before each test.

    Tests share a single SQLite DB (created at module import), so CRUD
    tests must clean up to avoid unique-constraint violations on
    (product_key, org_id).
    """
    db = SessionLocal()
    try:
        for model in (
            ForecastRun,
            ForecastAccuracyLog,
            ForecastBusinessRule,
            DomainPackInstall,
            ForecastTarget,
        ):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    yield


# ── Structural tests (no DB session needed) ─────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastBusinessRule, DomainPackInstall],
)
def test_all_models_inherit_timestamped_base(model):
    assert issubclass(model, TimestampedBase)


def test_forecast_target_tablename():
    assert ForecastTarget.__tablename__ == "forecast_targets"


def test_forecast_run_tablename():
    assert ForecastRun.__tablename__ == "forecast_runs"


def test_forecast_accuracy_log_tablename():
    assert ForecastAccuracyLog.__tablename__ == "forecast_accuracy_log"


def test_forecast_business_rule_tablename():
    assert ForecastBusinessRule.__tablename__ == "forecast_business_rules"


def test_domain_pack_install_tablename():
    assert DomainPackInstall.__tablename__ == "domain_pack_installs"


def test_forecast_target_has_required_columns():
    cols = {c.name for c in ForecastTarget.__table__.columns}
    required = {
        # TimestampedBase
        "id", "created_date", "updated_date", "created_by_id",
        "is_deleted", "org_id", "app_id",
        # Business
        "product_key", "name", "aliases", "datasource", "level",
        "quality_grade", "quality_stats", "status", "source",
        "model_config", "include_in_weekly_report", "report_order",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_forecast_run_has_required_columns():
    cols = {c.name for c in ForecastRun.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id",
        "is_deleted", "org_id", "app_id",
        "target_id", "results", "below_naive_baseline",
        "confidence", "as_of_date", "model_detail",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_forecast_accuracy_log_has_required_columns():
    cols = {c.name for c in ForecastAccuracyLog.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id",
        "is_deleted", "org_id", "app_id",
        "target_id", "horizon_days", "n_backtests",
        "window_start", "window_end",
        "mape", "naive_mape", "skill_vs_naive",
        "below_naive_baseline", "per_model",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_forecast_business_rule_has_required_columns():
    cols = {c.name for c in ForecastBusinessRule.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id",
        "is_deleted", "org_id", "app_id",
        "target_id", "rule_type", "params", "status", "source",
        "confidence", "evidence", "approved_by_id", "approved_at",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_domain_pack_install_has_required_columns():
    cols = {c.name for c in DomainPackInstall.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id",
        "is_deleted", "org_id", "app_id",
        "pack_key", "pack_version", "config", "installed_at",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


# ── Default value tests (the honesty gate) ────────────────────────────────────


def test_forecast_target_defaults():
    assert ForecastTarget.__table__.c.level.default.arg == 0
    assert ForecastTarget.__table__.c.status.default.arg == "discovered"
    assert ForecastTarget.__table__.c.source.default.arg == "discovery"
    assert ForecastTarget.__table__.c.include_in_weekly_report.default.arg is False


def test_forecast_run_below_naive_defaults_false():
    """The honesty gate must default to False — never silently ship below-naive."""
    assert ForecastRun.__table__.c.below_naive_baseline.default.arg is False


def test_forecast_accuracy_log_below_naive_defaults_false():
    assert ForecastAccuracyLog.__table__.c.below_naive_baseline.default.arg is False


def test_forecast_business_rule_defaults():
    assert ForecastBusinessRule.__table__.c.status.default.arg == "proposed"
    assert ForecastBusinessRule.__table__.c.source.default.arg == "chat"


# ── FK relationship tests ────────────────────────────────────────────────────


def test_forecast_run_fk_to_forecast_targets():
    fks = {fk.target_fullname for fk in ForecastRun.__table__.foreign_keys}
    assert "forecast_targets.id" in fks


def test_forecast_accuracy_log_fk_to_forecast_targets():
    fks = {fk.target_fullname for fk in ForecastAccuracyLog.__table__.foreign_keys}
    assert "forecast_targets.id" in fks


def test_forecast_business_rule_fk_to_forecast_targets():
    fks = {fk.target_fullname for fk in ForecastBusinessRule.__table__.foreign_keys}
    assert "forecast_targets.id" in fks


def test_forecast_business_rule_target_id_is_nullable():
    """Global guardrail rules have no target_id."""
    col = ForecastBusinessRule.__table__.c.target_id
    assert col.nullable is True


def test_domain_pack_install_has_no_foreign_keys():
    """domain_pack_installs is independent — no FK to any table."""
    assert len(DomainPackInstall.__table__.foreign_keys) == 0


# ── Unique constraint test ────────────────────────────────────────────────────


def test_forecast_target_unique_product_key_per_org():
    constraints = {
        c.name
        for c in ForecastTarget.__table__.constraints
        if hasattr(c, "columns") and len(c.columns) == 2
    }
    assert "uq_forecast_targets_product_key_org_id" in constraints


# ── CRUD tests ────────────────────────────────────────────────────────────────


def test_forecast_target_crud():
    db = SessionLocal()
    try:
        target = ForecastTarget(
            product_key="isoprene",
            name="Isoprene",
            aliases=["异戊二烯"],
            datasource={
                "table": "market_prices",
                "time_col": "date",
                "measure": "isoprene",
                "region": "east_china",
                "granularity": "daily",
            },
            level=2,
            quality_grade="A",
            quality_stats={
                "history_length": 95,
                "missing_ratio": 0.02,
                "outlier_ratio": 0.01,
                "stationarity": 0.03,
                "seasonality_strength": 0.65,
                "frequency_regularity": 0.12,
            },
            status="active",
            source="pack",
            include_in_weekly_report=True,
            report_order=1,
        )
        db.add(target)
        db.commit()
        db.refresh(target)

        assert target.id is not None
        assert target.product_key == "isoprene"
        assert target.name == "Isoprene"
        assert target.aliases == ["异戊二烯"]
        assert target.datasource["table"] == "market_prices"
        assert target.level == 2
        assert target.quality_grade == "A"
        assert target.quality_stats["history_length"] == 95
        assert target.status == "active"
        assert target.source == "pack"
        assert target.include_in_weekly_report is True
        assert target.report_order == 1
        assert target.org_id == "default-org"
        assert target.created_date is not None
    finally:
        db.close()


def test_forecast_run_crud_with_scenario_json():
    db = SessionLocal()
    try:
        target = ForecastTarget(product_key="c5_resin", name="C5 Resin")
        db.add(target)
        db.commit()
        db.refresh(target)

        run = ForecastRun(
            target_id=target.id,
            results={
                "3d": {"base": 1240, "bull": 1380, "bear": 1100},
                "7d": {"base": 8650, "bull": 9400, "bear": 7900},
                "30d": {"base": 37200, "bull": 41000, "bear": 33400},
            },
            below_naive_baseline=False,
            confidence="High",
            model_detail={
                "models_run": ["ets", "arima", "seasonal_naive"],
                "weights": {"ets": 0.4, "arima": 0.35, "seasonal_naive": 0.25},
            },
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        assert run.id is not None
        assert run.target_id == target.id
        assert run.results["7d"]["base"] == 8650
        assert run.results["30d"]["bull"] == 41000
        assert run.below_naive_baseline is False
        assert run.confidence == "High"
        assert run.model_detail["weights"]["ets"] == 0.4
    finally:
        db.close()


def test_forecast_run_below_naive_baseline_true():
    """When ensemble fails to beat naive, the flag must be settable to True."""
    db = SessionLocal()
    try:
        target = ForecastTarget(product_key="blowing_agent", name="Blowing Agent")
        db.add(target)
        db.commit()
        db.refresh(target)

        run = ForecastRun(
            target_id=target.id,
            results={"7d": {"base": 5000, "bull": 5500, "bear": 4500}},
            below_naive_baseline=True,
            confidence="Low",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        assert run.below_naive_baseline is True
    finally:
        db.close()


def test_forecast_accuracy_log_crud():
    db = SessionLocal()
    try:
        target = ForecastTarget(product_key="cracked_c9", name="Cracked C9")
        db.add(target)
        db.commit()
        db.refresh(target)

        log = ForecastAccuracyLog(
            target_id=target.id,
            horizon_days=7,
            n_backtests=5,
            window_start=None,
            window_end=None,
            mape=0.1043,
            naive_mape=0.0980,
            skill_vs_naive=0.0063,
            below_naive_baseline=True,
            per_model={
                "ets": 0.12,
                "arima": 0.15,
                "seasonal_naive": 0.098,
            },
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        assert log.id is not None
        assert log.target_id == target.id
        assert log.horizon_days == 7
        assert log.mape == pytest.approx(0.1043)
        assert log.naive_mape == pytest.approx(0.0980)
        assert log.skill_vs_naive == pytest.approx(0.0063)
        assert log.below_naive_baseline is True
        assert log.per_model["seasonal_naive"] == 0.098
    finally:
        db.close()


def test_forecast_business_rule_proposed_to_active_workflow():
    db = SessionLocal()
    try:
        target = ForecastTarget(product_key="c5_resin", name="C5 Resin")
        db.add(target)
        db.commit()
        db.refresh(target)

        # Rule starts as 'proposed' (from chat capture)
        rule = ForecastBusinessRule(
            target_id=target.id,
            rule_type="seasonal",
            params={"month": 11, "adjustment_pct": -2.5},
            status="proposed",
            source="chat",
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

        assert rule.status == "proposed"
        assert rule.source == "chat"
        assert rule.params["adjustment_pct"] == -2.5

        # Promote to 'active' (requires approval)
        rule.status = "active"
        rule.approved_by_id = "user-123"
        rule.confidence = 0.85
        rule.evidence = {"backtest_improvement": 0.03, "source": "historical_pattern"}
        db.commit()
        db.refresh(rule)

        assert rule.status == "active"
        assert rule.approved_by_id == "user-123"
        assert rule.confidence == pytest.approx(0.85)
        assert rule.evidence["backtest_improvement"] == 0.03
    finally:
        db.close()


def test_forecast_business_rule_global_guardrail_no_target():
    """Guardrail rules can have no target_id (apply to all targets)."""
    db = SessionLocal()
    try:
        rule = ForecastBusinessRule(
            target_id=None,
            rule_type="guardrail",
            params={"min_history": 14, "max_mape": 0.3},
            status="active",
            source="pack",
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

        assert rule.id is not None
        assert rule.target_id is None
        assert rule.rule_type == "guardrail"
        assert rule.params["min_history"] == 14
    finally:
        db.close()


def test_domain_pack_install_crud():
    db = SessionLocal()
    try:
        install = DomainPackInstall(
            pack_key="ecisco_c5c9",
            pack_version="1",
            config={
                "products": [
                    {"product_key": "isoprene", "name": "Isoprene"},
                ],
                "guardrails": {"min_history": 14},
            },
        )
        db.add(install)
        db.commit()
        db.refresh(install)

        assert install.id is not None
        assert install.pack_key == "ecisco_c5c9"
        assert install.pack_version == "1"
        assert install.config["products"][0]["product_key"] == "isoprene"
    finally:
        db.close()
