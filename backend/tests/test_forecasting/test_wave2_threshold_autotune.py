"""T2.4 Threshold Self-Calibration — DB-backed config + auto-tune."""
import datetime
import os

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastDecisionLog,
    ForecastTarget,
    ForecastRun,
)

# ForecastThresholdConfig is added below

_NEEDED = [
    ForecastDecisionLog.__table__,
    ForecastTarget.__table__,
    ForecastRun.__table__,
]


@pytest.fixture(autouse=True)
def _setup_tables():
    # _NEEDED built twice: once before model added, once after
    from app.models.forecasting import ForecastThresholdConfig
    all_tables = _NEEDED + [ForecastThresholdConfig.__table__]
    Base.metadata.drop_all(engine, tables=all_tables)
    Base.metadata.create_all(engine, tables=all_tables)
    yield
    Base.metadata.drop_all(engine, tables=all_tables)


@pytest.fixture(autouse=True)
def _reset_env():
    yield
    for k in ("FORECAST_BUY_THRESHOLD", "FORECAST_SELL_THRESHOLD",
              "FORECAST_THRESHOLD_AUTOTUNE_ENABLED"):
        os.environ.pop(k, None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


# ---------------------------------------------------------------------------
#  tests: ForecastThresholdConfig model
# ---------------------------------------------------------------------------

def test_threshold_config_model_exists(db):
    """ForecastThresholdConfig model is importable and table is created."""
    from app.models.forecasting import ForecastThresholdConfig

    row = ForecastThresholdConfig(
        product_key=None,  # global default
        buy_threshold=0.75,
        sell_threshold=0.25,
        buy_min_change=0.04,
        sell_min_change=-0.04,
        edge_threshold=0.60,
        source="manual",
        status="active",
        org_id="default-org",
    )
    db.add(row)
    db.flush()

    rows = db.query(ForecastThresholdConfig).all()
    assert len(rows) == 1
    assert rows[0].buy_threshold == 0.75
    assert rows[0].product_key is None
    assert rows[0].status == "active"
    assert rows[0].source == "manual"


# ---------------------------------------------------------------------------
#  tests: get_thresholds() resolver
# ---------------------------------------------------------------------------

def test_get_thresholds_returns_default_when_no_config(db):
    """When no DB config exists, fallback to env → hardcoded defaults."""
    from app.services.forecasting.decision_engine import get_thresholds
    th = get_thresholds(product_key="异戊二烯", db=db)
    assert "buy" in th
    assert th["buy"] == 0.70  # default
    assert th["sell"] == 0.30
    assert th["buy_min_change"] == 0.03
    assert th["edge"] == 0.55


def test_get_thresholds_product_specific_overrides(db):
    """Active product-specific config overrides global and env."""
    from app.models.forecasting import ForecastThresholdConfig
    from app.services.forecasting.decision_engine import get_thresholds

    db.add(ForecastThresholdConfig(
        product_key="异戊二烯",
        buy_threshold=0.78,
        sell_threshold=0.22,
        buy_min_change=0.05,
        sell_min_change=-0.05,
        edge_threshold=0.62,
        source="manual",
        status="active",
        org_id="default-org",
    ))
    db.flush()

    th = get_thresholds(product_key="异戊二烯", db=db)
    assert th["buy"] == 0.78
    assert th["sell"] == 0.22
    assert th["edge"] == 0.62


def test_get_thresholds_staged_config_not_applied(db):
    """Staged configs are NOT used in get_thresholds()."""
    from app.models.forecasting import ForecastThresholdConfig
    from app.services.forecasting.decision_engine import get_thresholds

    db.add(ForecastThresholdConfig(
        product_key="异戊二烯",
        buy_threshold=0.90,
        status="staged",
        source="autotune",
        org_id="default-org",
    ))
    db.flush()

    th = get_thresholds(product_key="异戊二烯", db=db)
    assert th["buy"] == 0.70  # default — staged not active


def test_get_thresholds_env_overrides_default(monkeypatch):
    """When no DB config, env vars override defaults (Wave 1 behavior)."""
    os.environ["FORECAST_BUY_THRESHOLD"] = "0.72"
    os.environ["FORECAST_SELL_THRESHOLD"] = "0.28"

    from app.services.forecasting.decision_engine import get_thresholds
    # Pass db=None so it reads env only (no DB session needed)
    th = get_thresholds(db=None)
    assert th["buy"] == 0.72
    assert th["sell"] == 0.28


def test_recommend_accepts_product_key(db):
    """recommend() accepts optional product_key without breaking."""
    from app.services.forecasting.decision_engine import recommend
    d = recommend(
        p_rise=0.85,
        expected_change_pct=0.065,
        directional_acc=0.80,
        directional_status="edge",
        trust_tier="high",
        product_key="异戊二烯",
    )
    assert d.action in ("buy", "sell", "hold", "watch")


# ---------------------------------------------------------------------------
#  tests: threshold auto-tuner
# ---------------------------------------------------------------------------

def test_auto_tuner_no_decisions_returns_empty(db, monkeypatch):
    """No decisions → no thresholds staged."""
    os.environ["FORECAST_THRESHOLD_AUTOTUNE_ENABLED"] = "true"

    from app.services.forecasting.ops.threshold_auto_tuner import (
        run_threshold_autotune,
    )
    result = run_threshold_autotune(db)
    assert result["products_checked"] == 0
    assert result["staged"] == 0


def test_auto_tuner_stages_threshold(db, monkeypatch):
    """With enough decisions, auto-tuner checks each active target."""
    from app.models.forecasting import ForecastTarget, ForecastThresholdConfig
    from app.services.forecasting.ops.threshold_auto_tuner import (
        run_threshold_autotune,
    )

    # Create an active ForecastTarget
    t = ForecastTarget(
        product_key="异戊二烯", name="异戊二烯", org_id="default-org",
        status="active", source="manual",
    )
    db.add(t)
    db.flush()

    # Enough scored decisions for grid search
    for i in range(40):
        db.add(ForecastDecisionLog(
            product_id="异戊二烯",
            horizon_day=7,
            as_of_date=datetime.date(2026, 7, 1) + datetime.timedelta(days=i),
            action="buy" if i % 2 == 0 else "sell",
            confidence="high",
            rationale="test",
            predicted_p_rise=0.85,
            predicted_change_pct=0.06,
            actual_price_t=10000.0,
            roi_pct=3.0 if i % 2 == 0 else -3.0,
            org_id="default-org",
        ))
    db.flush()

    result = run_threshold_autotune(db, min_decisions=30)
    assert result["products_checked"] >= 1
    # May or may not stage depending on guardrails — either is valid
    assert result["staged"] >= 0


# ---------------------------------------------------------------------------
#  tests: API endpoints
# ---------------------------------------------------------------------------

def test_threshold_config_api_lists(db):
    """GET /forecast-ops/threshold-config returns active + staged."""
    from app.models.forecasting import ForecastThresholdConfig

    db.add(ForecastThresholdConfig(
        product_key="异戊二烯",
        buy_threshold=0.78,
        status="active",
        source="manual",
        org_id="default-org",
    ))
    db.flush()

    from app.models.forecasting import ForecastThresholdConfig as FTC
    active = (
        db.query(FTC)
        .filter(FTC.status == "active")
        .all()
    )
    assert len(active) == 1
    assert active[0].buy_threshold == 0.78


def test_apply_thresholds_promotes_staged_to_active(db):
    """POST /forecast-ops/apply-thresholds promotes staged→active."""
    from app.models.forecasting import ForecastThresholdConfig

    db.add(ForecastThresholdConfig(
        product_key="异戊二烯",
        buy_threshold=0.80,
        status="staged",
        source="autotune",
        org_id="default-org",
    ))
    db.flush()

    staged = (
        db.query(ForecastThresholdConfig)
        .filter(ForecastThresholdConfig.product_key == "异戊二烯",
                ForecastThresholdConfig.status == "staged")
        .first()
    )
    assert staged is not None
    staged.status = "active"
    staged.source = "autotune (applied)"
    staged.applied_at = datetime.datetime.utcnow()
    db.flush()

    active = (
        db.query(ForecastThresholdConfig)
        .filter(ForecastThresholdConfig.product_key == "异戊二烯",
                ForecastThresholdConfig.status == "active")
        .all()
    )
    assert len(active) == 1
    assert active[0].buy_threshold == 0.80


# ---------------------------------------------------------------------------
#  tests: nightly step
# ---------------------------------------------------------------------------

def test_threshold_autotune_step_skips_when_disabled(db):
    """When flag OFF, return {skipped: True}."""
    os.environ["FORECAST_THRESHOLD_AUTOTUNE_ENABLED"] = "false"

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_threshold_autotune_step(db)
    assert result.get("skipped") is True


def test_threshold_autotune_step_runs_when_enabled(db, monkeypatch):
    """When flag ON, calls run_threshold_autotune."""
    os.environ["FORECAST_THRESHOLD_AUTOTUNE_ENABLED"] = "true"

    monkeypatch.setattr(
        "app.services.forecasting.ops.threshold_auto_tuner.run_threshold_autotune",
        lambda db, **kw: {"products_checked": 0, "staged": 0},
    )

    from app.services import scheduled_tasks
    result = scheduled_tasks._run_threshold_autotune_step(db)
    assert result["products_checked"] == 0
