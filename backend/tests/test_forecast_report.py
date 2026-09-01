"""Tests for weekly report pipeline (Section 4).

Validates: WeeklyReportGenerator (data assembly, markdown rendering,
honesty-gate surfacing, missing-forecast warnings) and the forecast_report
tool handler (dispatch, schema, artifact persistence, error cases).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
    ForecastBusinessRule,
)
from app.models.artifact import Artifact
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.forecasting.report import (
    WeeklyReportGenerator,
    WeeklyReport,
    ProductReport,
    _label,
    _fmt_val,
)
from app.services.tool_handlers.forecast_tool import (
    _forecast_report,
    FORECAST_REPORT_SCHEMA,
    _resolve_org_context,
)

# ── Fixtures ──────────────────────────────────────────────────────────

_NEEDED_TABLES = [
    ForecastTarget.__table__,
    ForecastRun.__table__,
    ForecastAccuracyLog.__table__,
    ForecastBusinessRule.__table__,
    Artifact.__table__,
    KnowledgeBase.__table__,
    User.__table__,
]


@pytest.fixture(autouse=True)
def _migrate_schema():
    """Drop and recreate needed tables before each test for isolation."""
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)
    Base.metadata.create_all(engine, tables=_NEEDED_TABLES)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)


@pytest.fixture
def db():
    """Clean DB session (rollback after each test)."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def org_context():
    return {"org_id": "test-org", "app_id": "test-app"}


@pytest.fixture
def user_id():
    return "user-001"


def _seed_target(
    db,
    *,
    target_id: str = "target-001",
    name: str = "Daily Sales",
    product_key: str = "sales_daily",
    quality_grade: str = "B",
    include_in_weekly_report: bool = True,
    report_order: int | None = None,
    status: str = "active",
):
    """Create a single ForecastTarget and commit it."""
    t = ForecastTarget(
        id=target_id,
        org_id="test-org",
        app_id="test-app",
        product_key=product_key,
        name=name,
        level="product",
        source="forecast_discover",
        status=status,
        quality_grade=quality_grade,
        include_in_weekly_report=include_in_weekly_report,
        report_order=report_order,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


_MD_SENTINEL = object()


def _seed_run(
    db,
    target_id: str = "target-001",
    below_naive_baseline: bool = False,
    confidence: str = "high",
    results: dict | None = None,
    model_detail: Any = _MD_SENTINEL,
):
    """Create a single ForecastRun and commit it.

    Pass ``model_detail=None`` explicitly to store None (omit footer).
    Omit the arg to get the default ensemble details.
    """
    if results is None:
        results = {
            "3": {"base": [100, 102, 104], "bull": [105, 108, 111], "bear": [95, 96, 97]},
            "7": {"base": [100, 102, 104, 106, 108, 110, 112], "bull": [108, 112, 116, 120, 124, 128, 132], "bear": [92, 94, 96, 98, 100, 102, 104]},
            "30": {"base": [100, 103, 106, 109, 112], "bull": [102, 106, 110, 114, 118], "bear": [98, 100, 102, 104, 106]},
        }
    if model_detail is _MD_SENTINEL:
        model_detail = {
            "models_run": ["ets", "arima", "prophet"],
            "weights": {"ets": 0.4, "arima": 0.35, "prophet": 0.25},
            "failed": [],
        }
    r = ForecastRun(
        org_id="test-org",
        app_id="test-app",
        target_id=target_id,
        results=results,
        below_naive_baseline=below_naive_baseline,
        confidence=confidence,
        model_detail=model_detail,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _seed_accuracy(db, target_id: str = "target-001"):
    """Create ForecastAccuracyLog rows for 3 horizons."""
    entries = []
    for hd, mape, naive_mape, skill in [
        (3, 0.08, 0.12, 0.33),
        (7, 0.11, 0.15, 0.27),
        (30, 0.18, 0.22, 0.18),
    ]:
        entry = ForecastAccuracyLog(
            org_id="test-org",
            app_id="test-app",
            target_id=target_id,
            horizon_days=hd,
            mape=mape,
            naive_mape=naive_mape,
            skill_vs_naive=skill,
            below_naive_baseline=False,
            per_model={"ets": mape - 0.01, "arima": mape + 0.02},
        )
        db.add(entry)
        entries.append(entry)
    db.commit()
    for e in entries:
        db.refresh(e)
    return entries


# Set db to in-memory SQLite
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ======================================================================
# Report generator tests
# ======================================================================


class TestWeeklyReportGenerator:
    """WeeklyReportGenerator: data assembly and markdown rendering."""

    # ── Empty / no-targets ──────────────────────────────────────

    def test_empty_report(self, db):
        """When no targets have include_in_weekly_report=True, the report
        should still succeed with zero products."""
        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        assert isinstance(report, WeeklyReport)
        assert report.org_id == "test-org"
        assert report.summary["total"] == 0
        assert report.summary["below_baseline"] == 0
        assert report.products == []

        md = report.markdown
        assert "# Weekly Forecast Brief" in md
        assert "Executive Summary" in md
        assert "**0** products tracked" in md

    def test_targets_not_in_report_still_excluded(self, db):
        """Targets without include_in_weekly_report should be excluded."""
        _seed_target(db, include_in_weekly_report=False)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        assert report.summary["total"] == 0

    # ── Single product ─────────────────────────────────────────

    def test_single_product_with_forecast(self, db):
        """A single product with cached forecast produces a full report."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        assert report.summary["total"] == 1
        assert report.summary["below_baseline"] == 0
        assert report.summary["confidence_dist"]["high"] == 1
        assert len(report.products) == 1

        p = report.products[0]
        assert p.name == "Daily Sales"
        assert p.product_key == "sales_daily"
        assert p.quality_grade == "B"
        assert p.confidence == "high"
        assert p.below_naive_baseline is False
        assert p.has_forecast is True
        assert len(p.results) == 3  # 3 horizons

    # ── Multiple products ──────────────────────────────────────

    def test_multiple_products(self, db):
        """Multiple targets with forecasts produce ordered report sections."""
        _seed_target(db, target_id="t1", name="Product A", product_key="prod_a", report_order=2)
        _seed_target(db, target_id="t2", name="Product B", product_key="prod_b", report_order=1)
        _seed_run(db, target_id="t1", results={"3": {"base": [10, 12, 14]}})
        _seed_run(db, target_id="t2", results={"3": {"base": [20, 22, 24]}})

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        assert report.summary["total"] == 2
        assert len(report.products) == 2
        # Ordered by report_order: B (1) then A (2)
        assert report.products[0].name == "Product B"
        assert report.products[1].name == "Product A"

    def test_multiple_products_by_name_fallback(self, db):
        """When report_order is null, products sort by name."""
        _seed_target(db, target_id="t1", name="Zebra", product_key="pk_zebra", report_order=None)
        _seed_target(db, target_id="t2", name="Alpha", product_key="pk_alpha", report_order=None)
        _seed_run(db, target_id="t1", results={"3": {"base": [1, 2, 3]}})
        _seed_run(db, target_id="t2", results={"3": {"base": [4, 5, 6]}})

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        assert report.products[0].name == "Alpha"
        assert report.products[1].name == "Zebra"

    # ── Honesty gate ───────────────────────────────────────────

    def test_honesty_gate_pass(self, db):
        """Product passing honesty gate shows PASS badge."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=False)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        p = report.products[0]
        assert p.below_naive_baseline is False
        assert "Honesty gate: PASS" in p.markdown_section
        assert "WARNING" not in p.markdown_section

    def test_honesty_gate_fail(self, db):
        """Product failing honesty gate shows WARNING callout."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=True, confidence="low")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        p = report.products[0]
        assert p.below_naive_baseline is True
        assert "WARNING: Below naive baseline" in p.markdown_section
        assert report.summary["below_baseline"] == 1
        assert report.summary["confidence_dist"]["low"] == 1

    # ── Missing forecast ───────────────────────────────────────

    def test_target_without_forecast(self, db):
        """Target with no cached ForecastRun produces a warning section."""
        _seed_target(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        assert report.summary["total"] == 1
        p = report.products[0]
        assert p.has_forecast is False
        assert p.results is None
        assert "No forecast available" in p.markdown_section
        assert p.confidence is None
        assert report.summary["confidence_dist"]["none"] == 1

    def test_mixed_forecast_availability(self, db):
        """Some targets have forecasts, some don't — report handles both."""
        _seed_target(db, target_id="t1", name="With Forecast", product_key="pk_with")
        _seed_target(db, target_id="t2", name="Without Forecast", product_key="pk_without")
        _seed_run(db, target_id="t1")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        assert len(report.products) == 2
        with_f = next(p for p in report.products if p.name == "With Forecast")
        without_f = next(p for p in report.products if p.name == "Without Forecast")
        assert with_f.has_forecast is True
        assert without_f.has_forecast is False

    # ── Markdown structure ─────────────────────────────────────

    def test_markdown_contains_essential_sections(self, db):
        """Generated markdown includes all required sections."""
        _seed_target(db)
        _seed_run(db, results={"3": {"base": [100, 102, 104], "bull": [105, 108, 111], "bear": [95, 96, 97]}})
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        md = report.markdown

        assert "# Weekly Forecast Brief" in md
        assert "## Executive Summary" in md
        assert "## Daily Sales" in md
        assert "Honesty gate: PASS" in md
        assert "### 3-Day Forecast" in md
        assert "Accuracy" in md
        assert "Model Detail" in md

    def test_markdown_scenario_table_columns(self, db):
        """Scenario tables render columns for each horizon day."""
        _seed_target(db)
        _seed_run(db, results={
            "3": {"base": [100, 102, 104], "bull": [105, 108, 111], "bear": [95, 96, 97]},
        })

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        md = report.products[0].markdown_section

        assert "Day 1" in md
        assert "Day 2" in md
        assert "Day 3" in md
        assert "Base" in md
        assert "Bull" in md
        assert "Bear" in md
        # Check rendered values
        assert "100" in md
        assert "105" in md

    def test_markdown_confidence_distribution(self, db):
        """Executive summary captures confidence distribution across products."""
        _seed_target(db, target_id="t-high", name="High", product_key="pk_high", quality_grade="A")
        _seed_target(db, target_id="t-med", name="Med", product_key="pk_med", quality_grade="B")
        _seed_target(db, target_id="t-low", name="Low", product_key="pk_low", quality_grade="D")
        _seed_run(db, target_id="t-high", confidence="high")
        _seed_run(db, target_id="t-med", confidence="medium")
        _seed_run(db, target_id="t-low", confidence="low")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")

        cdist = report.summary["confidence_dist"]
        assert cdist["high"] == 1
        assert cdist["medium"] == 1
        assert cdist["low"] == 1

    # ── Model detail rendering ─────────────────────────────────

    def test_model_detail_in_markdown(self, db):
        """Model detail (models run, weights, failed) appears in markdown."""
        _seed_target(db)
        _seed_run(
            db,
            model_detail={
                "models_run": ["ets", "arima"],
                "weights": {"ets": 0.5, "arima": 0.5},
                "failed": ["prophet"],
            },
        )

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        md = report.products[0].markdown_section

        assert "Models run: ets, arima" in md
        assert "ets: 0.50" in md
        assert "Models failed: prophet" in md

    def test_model_detail_none(self, db):
        """When model_detail is None, the section is omitted."""
        _seed_target(db)
        _seed_run(db, model_detail=None)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        md = report.products[0].markdown_section
        assert "Model Detail" not in md

    # ── Accuracy rendering ─────────────────────────────────────

    def test_accuracy_table_in_markdown(self, db):
        """Accuracy metrics table renders with MAPE and skill values."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        md = report.products[0].markdown_section

        assert "MAPE" in md
        assert "Naive MAPE" in md
        assert "Skill" in md
        assert "3 days" in md
        assert "7 days" in md
        assert "8.0%" in md or "0.080" in md  # MAPE as percentage


# ======================================================================
# Helpers tests
# ======================================================================


class TestHelperFunctions:
    def test_label_known_horizons(self):
        assert _label("3") == "3-Day"
        assert _label("3d") == "3-Day"
        assert _label("7") == "7-Day"
        assert _label("7d") == "7-Day"
        assert _label("30") == "30-Day"
        assert _label("30d") == "30-Day"

    def test_label_unknown(self):
        assert _label("14") == "14-Day"

    def test_fmt_val(self):
        assert _fmt_val(100) == "100"
        assert _fmt_val(1234) == "1,234"
        assert _fmt_val(3.14159) == "3.1"
        assert _fmt_val(None) == "N/A"
        assert _fmt_val("hello") == "hello"


# ======================================================================
# forecast_report tool tests
# ======================================================================


class TestForecastReportSchema:
    """Schema must match OpenAI function-calling format."""

    def test_schema_structure(self):
        assert FORECAST_REPORT_SCHEMA["type"] == "function"
        fn = FORECAST_REPORT_SCHEMA["function"]
        assert fn["name"] == "forecast_report"
        assert isinstance(fn["description"], str) and len(fn["description"]) > 10
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params

    def test_schema_action_enum(self):
        props = FORECAST_REPORT_SCHEMA["function"]["parameters"]["properties"]
        assert "action" in props
        assert props["action"]["enum"] == ["generate", "get"]

    def test_schema_save_artifact(self):
        props = FORECAST_REPORT_SCHEMA["function"]["parameters"]["properties"]
        assert "save_artifact" in props
        assert props["save_artifact"]["type"] == "boolean"


class TestForecastReportHandler:
    """Tool handler: dispatch, artifact persistence, error cases."""

    # ── Generate action ────────────────────────────────────────

    def test_generate_success(self, db, org_context, user_id):
        """Generate a report from cached forecasts."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        result = _forecast_report(
            args={"action": "generate"},
            db=db,
            user_id=user_id,
            context=org_context,
        )
        # Handler is async — call via asyncio
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate"},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert "report" in result
        assert result["report"]["products_count"] == 1
        assert result["report"]["summary"]["total"] == 1
        assert len(result["report"]["markdown"]) > 100

    def test_generate_empty(self, db, org_context, user_id):
        """Generate succeeds even with no targets."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate"},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert result["report"]["products_count"] == 0

    # ── Get action (stub) ──────────────────────────────────────

    def test_get_stub(self, db, org_context, user_id):
        """Get action returns stub message (artifact retrieval not yet wired)."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "get"},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert "message" in result
        assert "action='generate'" in result["message"] or "use action" in result.get("message", "").lower()

    # ── Default action ─────────────────────────────────────────

    def test_default_action_is_generate(self, db, org_context, user_id):
        """Omitting action defaults to 'generate'."""
        _seed_target(db)
        _seed_run(db)

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert result["report"]["products_count"] == 1

    # ── Unknown action ─────────────────────────────────────────

    def test_unknown_action(self, db, org_context, user_id):
        """Unknown action returns an error."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "invalid"},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is False
        assert "Unknown action" in result["error"]

    # ── Artifact persistence ───────────────────────────────────

    def test_save_artifact_generates_artifact_id(self, db, org_context, user_id):
        """When save_artifact=True, an artifact is persisted."""
        _seed_target(db)
        _seed_run(db)

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate", "save_artifact": True},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert "artifact_id" in result
        assert result["artifact_id"]
        # Verify artifact exists in DB
        artifact = db.get(Artifact, result["artifact_id"])
        assert artifact is not None
        assert artifact.artifact_type == "md"
        assert "Weekly Forecast Brief" in artifact.title

    # ── Org context resolution ─────────────────────────────────

    def test_uses_org_from_context(self, db, user_id):
        """Report uses org_id from the agent context."""
        _seed_target(db, include_in_weekly_report=True)

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate"},
                    db=db,
                    user_id=user_id,
                    context={"org_id": "test-org"},
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert result["report"]["products_count"] == 1

    def test_different_org_excluded(self, db, user_id):
        """Targets in other orgs are excluded from report."""
        _seed_target(db, include_in_weekly_report=True)  # org_id="test-org"

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate"},
                    db=db,
                    user_id=user_id,
                    context={"org_id": "other-org"},
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert result["report"]["products_count"] == 0

    # ── Honesty gate in tool response ──────────────────────────

    def test_honesty_gate_surfaced_in_response(self, db, org_context, user_id):
        """Honesty gate flag appears in the tool response markdown."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=True, confidence="low")

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _forecast_report(
                    args={"action": "generate"},
                    db=db,
                    user_id=user_id,
                    context=org_context,
                )
            )
        finally:
            loop.close()

        assert result["success"] is True
        assert "WARNING: Below naive baseline" in result["report"]["markdown"]
        assert result["report"]["summary"]["below_baseline"] == 1
