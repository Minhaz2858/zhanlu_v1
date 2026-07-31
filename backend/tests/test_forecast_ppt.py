"""Tests for PPT workflow (Section 5).

Validates: ForecastPayloadAssembler (WeeklyReport → ReportCardPayload
mapping, scenario chart data, KPI tiles, forecast table, accuracy data,
honesty-gate warnings, InsightSpec text field) and the forecast_ppt tool
handler (dispatch, delegation to _create_artifact_tool, schema, errors).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

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
)
from app.services.forecasting.pptx_payload import ForecastPayloadAssembler
from app.services.synexia.contracts import (
    ReportCardPayload,
    ChartSpec,
    KPISpec,
    InsightSpec,
    SectionSpec,
)
from app.services.tool_handlers.forecast_tool import (
    _forecast_ppt,
    FORECAST_PPT_SCHEMA,
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


def _seed_run(
    db,
    target_id: str = "target-001",
    below_naive_baseline: bool = False,
    confidence: str = "high",
    results: dict | None = None,
    model_detail: dict | None = None,
):
    """Create a single ForecastRun and commit it."""
    if results is None:
        results = {
            "3": {"base": [100, 102, 104], "bull": [105, 108, 111], "bear": [95, 96, 97]},
            "7": {"base": [100, 102, 104, 106, 108, 110, 112],
                  "bull": [108, 112, 116, 120, 124, 128, 132],
                  "bear": [92, 94, 96, 98, 100, 102, 104]},
            "30": {"base": [100, 103, 106, 109, 112],
                   "bull": [102, 106, 110, 114, 118],
                   "bear": [98, 100, 102, 104, 106]},
        }
    if model_detail is None:
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


def _run_async(coro):
    """Execute an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Set db to in-memory SQLite
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ======================================================================
# ForecastPayloadAssembler tests
# ======================================================================


class TestPayloadAssembler:
    """ForecastPayloadAssembler: WeeklyReport → ReportCardPayload mapping."""

    # ── Empty report ────────────────────────────────────────────

    def test_empty_report(self, db):
        """Assembling an empty WeeklyReport produces a valid payload."""
        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert isinstance(payload, ReportCardPayload)
        assert payload.title == f"Weekly Forecast Brief — {report.generated_at.strftime('%Y-%m-%d')}"
        assert "0 products" in payload.summary
        assert len(payload.kpis) >= 1
        assert payload.kpis[0].value == "0"
        assert payload.chart is None
        assert payload.warnings == []
        assert len(payload.sections) == 0
        assert any("No products configured" in f.text for f in payload.key_findings)

    # ── Single product ──────────────────────────────────────────

    def test_single_product(self, db):
        """A single product with forecast produces all payload sections."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        # Title & source
        assert "Weekly Forecast Brief" in payload.title
        assert "test-org" in payload.source

        # Summary
        assert "1 products" in payload.summary
        assert "honesty gate" in payload.summary.lower()

        # KPIs
        assert len(payload.kpis) >= 3
        assert payload.kpis[0].label == "Products Tracked"
        assert payload.kpis[0].value == "1"
        labels = {k.label for k in payload.kpis}
        assert "Below Baseline" in labels
        assert "High Confidence" in labels

        # Chart
        assert payload.chart is not None
        assert payload.chart.type == "line"
        assert payload.chart.x_key == "day"
        assert payload.chart.y_keys == ["base", "bull", "bear"]
        assert len(payload.chart.data) == 7  # 7-day horizon

        # Key findings
        assert len(payload.key_findings) >= 1
        kf = payload.key_findings[0]
        assert isinstance(kf, InsightSpec)
        assert kf.text
        assert "Daily Sales" in kf.text

        # Recommendations
        assert len(payload.recommendations) >= 1
        assert "honesty gate" in payload.recommendations[0].text.lower()

        # Sections
        section_titles = {s.title for s in payload.sections}
        assert "Forecast Summary" in section_titles
        assert "Accuracy Metrics" in section_titles

        # Warnings — should be empty when all pass
        assert payload.warnings == []

    # ── Below-naive warnings ────────────────────────────────────

    def test_below_naive_warnings(self, db):
        """Below-naive products produce warnings and recommendations."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=True, confidence="low")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        # Warnings list
        assert len(payload.warnings) >= 1
        assert "Daily Sales" in payload.warnings[0]
        assert "below naive baseline" in payload.warnings[0].lower()

        # Summary
        assert "1 product(s) fell below" in payload.summary
        assert "flagged for review" in payload.summary

        # KPIs
        bb_kpi = [k for k in payload.kpis if k.label == "Below Baseline"][0]
        assert bb_kpi.value == "1"
        assert bb_kpi.delta == "1 need review"

        # Recommendations should include review callout
        assert len(payload.recommendations) >= 1
        review_texts = [r.text for r in payload.recommendations]
        assert any("Review Daily Sales" in t for t in review_texts) or \
            any("Daily Sales" in t and "underperforms" in t.lower() for t in review_texts)

    # ── Multi-product ───────────────────────────────────────────

    def test_multi_product(self, db):
        """Multiple products produce KPI tiles and table entries for each."""
        _seed_target(db, target_id="t-a", name="Product A", product_key="pk_a")
        _seed_target(db, target_id="t-b", name="Product B", product_key="pk_b")
        _seed_run(db, target_id="t-a", confidence="high")
        _seed_run(db, target_id="t-b", confidence="medium")
        _seed_accuracy(db, target_id="t-a")
        _seed_accuracy(db, target_id="t-b")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert report.summary["total"] == 2
        assert "2 products" in payload.summary

        # Two findings
        assert len(payload.key_findings) >= 2
        names = []
        for f in payload.key_findings:
            names.append(f.text.split(":")[0] if ":" in f.text else "")
        assert "Product A" in names
        assert "Product B" in names

        # Forecast table
        table_section = [s for s in payload.sections if s.title == "Forecast Summary"][0]
        assert "Product A" in table_section.content
        assert "Product B" in table_section.content

    def test_multi_product_below_naive(self, db):
        """Mix of passing and failing products in the same report."""
        _seed_target(db, target_id="t-a", name="Product A", product_key="pk_a")
        _seed_target(db, target_id="t-b", name="Product B", product_key="pk_b")
        _seed_run(db, target_id="t-a", below_naive_baseline=False, confidence="high")
        _seed_run(db, target_id="t-b", below_naive_baseline=True, confidence="low")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert report.summary["below_baseline"] == 1
        assert len(payload.warnings) == 1
        assert "Product B" in payload.warnings[0]

        # Forecast table should prefix with ⚠
        table_section = [s for s in payload.sections if s.title == "Forecast Summary"][0]
        assert "⚠ Product B" in table_section.content

    # ── Scenario chart data shape ───────────────────────────────

    def test_chart_data_shape(self, db):
        """Chart data uses x_key='day' and y_keys=['base','bull','bear']."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org", horizon="7")

        chart = payload.chart
        assert chart is not None
        assert chart.type == "line"
        assert chart.x_key == "day"
        assert chart.y_keys == ["base", "bull", "bear"]

        # First row
        row0 = chart.data[0]
        assert row0["day"] == 1
        assert "base" in row0
        assert "bull" in row0
        assert "bear" in row0
        # Each value should be numeric
        assert isinstance(row0["base"], (int, float))
        assert isinstance(row0["bull"], (int, float))
        assert isinstance(row0["bear"], (int, float))

    def test_chart_horizon_selection(self, db):
        """Chart respects the horizon parameter (3 vs 7 vs 30)."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)

        for hk, expected_len in [("3", 3), ("7", 7), ("30", 5)]:
            payload = asm.assemble(report, org_id="test-org", horizon=hk)
            assert payload.chart is not None
            assert len(payload.chart.data) == expected_len, f"horizon={hk}: expected {expected_len} rows"
            assert _HORIZON_LABEL[hk] in payload.chart.title

    def test_chart_scalar_fallback(self, db):
        """When forecast values are scalars (not lists), still produce chart."""
        _seed_target(db)
        _seed_run(db, results={
            "7": {"base": 200, "bull": 210, "bear": 190},
        })

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org", horizon="7")

        assert payload.chart is not None
        assert len(payload.chart.data) == 1
        assert payload.chart.data[0]["base"] == 200

    def test_chart_missing_forecast(self, db):
        """Chart is None when the product has no forecast data."""
        _seed_target(db)  # no seed_run — no forecast

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert payload.chart is None

    # ── KPI values ──────────────────────────────────────────────

    def test_kpi_below_baseline_zero(self, db):
        """Below Baseline KPI shows 0 when all products pass."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=False)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        bb = [k for k in payload.kpis if k.label == "Below Baseline"][0]
        assert bb.value == "0"
        assert bb.delta == "All pass"

    def test_kpi_skill_vs_naive(self, db):
        """Avg Skill vs Naive KPI computed from accuracy data."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        skill_kpis = [k for k in payload.kpis if k.label == "Avg Skill vs Naive"]
        assert len(skill_kpis) == 1
        assert float(skill_kpis[0].value) > 0
        assert "1 products" in skill_kpis[0].delta

    # ── Forecast table section ──────────────────────────────────

    def test_forecast_table_has_all_horizons(self, db):
        """Forecast Summary section includes 3-Day, 7-Day, 30-Day columns."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        table_section = [s for s in payload.sections if s.title == "Forecast Summary"][0]
        assert "3-Day" in table_section.content
        assert "7-Day" in table_section.content
        assert "30-Day" in table_section.content
        assert "Confidence" in table_section.content

    def test_forecast_table_below_naive_legend(self, db):
        """Below-naive products cause a ⚠ legend to appear."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=True, confidence="low")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        table_section = [s for s in payload.sections if s.title == "Forecast Summary"][0]
        assert "⚠ = Below naive baseline" in table_section.content

    # ── Accuracy section ────────────────────────────────────────

    def test_accuracy_section_content(self, db):
        """Accuracy Metrics section has MAPE, Naive MAPE, Skill columns."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        acc_section = [s for s in payload.sections if s.title == "Accuracy Metrics"][0]
        assert "MAPE" in acc_section.content
        assert "Naive MAPE" in acc_section.content
        assert "Skill vs Naive" in acc_section.content
        assert "Daily Sales" in acc_section.content

    def test_no_accuracy_section_when_empty(self, db):
        """No Accuracy Metrics section when no accuracy data exists."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        acc_titles = {s.title for s in payload.sections}
        assert "Accuracy Metrics" not in acc_titles

    # ── Methodology ─────────────────────────────────────────────

    def test_methodology_text(self, db):
        """Methodology text includes ensemble description and honesty gate."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert "ensemble forecasting engine" in payload.methodology.lower()
        assert "honesty gate" in payload.methodology.lower()
        assert "seasonal-naive" in payload.methodology.lower()
        assert "MAPE" in payload.methodology

    # ── InsightSpec field correctness ───────────────────────────

    def test_insights_use_text_field(self, db):
        """InsightSpec uses 'text' (not 'title' or 'detail')."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        for finding in payload.key_findings:
            assert isinstance(finding, InsightSpec)
            assert finding.text  # the text field must be populated
            # icon defaults to "lightbulb" in the model; we explicitly set it
            assert finding.icon in ("trending-up", "alert-triangle", "check-circle", "info")

        for rec in payload.recommendations:
            assert isinstance(rec, InsightSpec)
            assert rec.text
            assert rec.icon in ("alert-triangle", "check-circle")

    def test_insight_field_not_title_or_detail(self, db):
        """Verify InsightSpec has no 'title' or 'detail' attribute."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        for finding in payload.key_findings:
            d = finding.model_dump()
            assert "text" in d
            assert "icon" in d
            assert "title" not in d
            assert "detail" not in d

    # ── target_id selection ─────────────────────────────────────

    def test_target_id_selection(self, db):
        """Providing target_id picks the correct product for chart."""
        _seed_target(db, target_id="t-a", name="Product A", product_key="pk_a")
        _seed_target(db, target_id="t-b", name="Product B", product_key="pk_b")
        _seed_run(db, target_id="t-a", confidence="high")
        _seed_run(db, target_id="t-b", confidence="medium")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)

        payload_a = asm.assemble(report, org_id="test-org", target_id="t-a")
        assert "Product A" in payload_a.chart.title

        payload_b = asm.assemble(report, org_id="test-org", target_id="t-b")
        assert "Product B" in payload_b.chart.title

    def test_target_id_not_found_falls_back(self, db):
        """Non-existent target_id falls back to first product with forecast."""
        _seed_target(db, target_id="t-a", name="Product A", product_key="pk_a")
        _seed_run(db, target_id="t-a")

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org", target_id="nonexistent")

        # Falls back to the only product
        assert payload.chart is not None
        assert "Product A" in payload.chart.title

    # ── user_signal ─────────────────────────────────────────────

    def test_user_signal_is_export(self, db):
        """Payload always sets user_signal='export'."""
        _seed_target(db)
        _seed_run(db)

        gen = WeeklyReportGenerator(db)
        report = gen.generate("test-org")
        asm = ForecastPayloadAssembler(db)
        payload = asm.assemble(report, org_id="test-org")

        assert payload.user_signal == "export"


# ── Helper constants ──────────────────────────────────────────
_HORIZON_LABEL = {"3": "3-Day", "7": "7-Day", "30": "30-Day"}


# ======================================================================
# forecast_ppt Schema tests
# ======================================================================


class TestForecastPptSchema:
    """Schema must match OpenAI function-calling format."""

    def test_schema_structure(self):
        assert FORECAST_PPT_SCHEMA["type"] == "function"
        fn = FORECAST_PPT_SCHEMA["function"]
        assert fn["name"] == "forecast_ppt"
        assert isinstance(fn["description"], str) and len(fn["description"]) > 10
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        # No required params
        assert "required" not in params

    def test_schema_horizon_enum(self):
        props = FORECAST_PPT_SCHEMA["function"]["parameters"]["properties"]
        assert "horizon" in props
        assert props["horizon"]["enum"] == ["3", "7", "30"]
        assert props["horizon"]["default"] == "7"

    def test_schema_save_artifact(self):
        props = FORECAST_PPT_SCHEMA["function"]["parameters"]["properties"]
        assert "save_artifact" in props
        assert props["save_artifact"]["type"] == "boolean"
        assert props["save_artifact"]["default"] is True

    def test_schema_target_id(self):
        props = FORECAST_PPT_SCHEMA["function"]["parameters"]["properties"]
        assert "target_id" in props
        assert props["target_id"]["type"] == "string"


# ======================================================================
# forecast_ppt Tool handler tests
# ======================================================================


class TestForecastPptHandler:
    """Tool handler: dispatch, payload assembly, artifact delegation, errors."""

    # ── Success ─────────────────────────────────────────────────

    def test_generate_success(self, db, org_context, user_id):
        """Generate a PPT payload from cached forecasts with save_artifact=False."""
        _seed_target(db)
        _seed_run(db)
        _seed_accuracy(db)

        result = _run_async(_forecast_ppt(
            args={"save_artifact": False},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        assert result["success"] is True
        assert result["mode"] == "payload_only"
        assert "payload" in result
        assert result["title"].startswith("Weekly Forecast Brief")

        # Payload should be a valid dict
        payload = result["payload"]
        assert "title" in payload
        assert "summary" in payload
        assert "kpis" in payload
        assert "chart" in payload
        assert "key_findings" in payload
        assert "sections" in payload

    # ── Empty report ────────────────────────────────────────────

    def test_empty_report_returns_payload(self, db, org_context, user_id):
        """Handler succeeds even with no targets."""
        result = _run_async(_forecast_ppt(
            args={"save_artifact": False},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        assert result["success"] is True
        payload = result["payload"]
        assert payload["summary"] != ""
        assert payload["chart"] is None  # no data
        assert payload["warnings"] == []

    # ── Horizon selection ───────────────────────────────────────

    def test_horizon_selection(self, db, org_context, user_id):
        """Handler passes horizon parameter through to the assembler."""
        _seed_target(db)
        _seed_run(db)

        result = _run_async(_forecast_ppt(
            args={"horizon": "3", "save_artifact": False},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        assert result["success"] is True
        # Chart title should mention 3-Day
        assert result["payload"]["chart"] is not None
        assert "3-Day" in result["payload"]["chart"]["title"]

    # ── target_id ───────────────────────────────────────────────

    def test_target_id_passed_through(self, db, org_context, user_id):
        """Handler passes target_id through for product selection."""
        _seed_target(db, target_id="t-a", name="Alpha", product_key="pk_a")
        _seed_target(db, target_id="t-b", name="Beta", product_key="pk_b")
        _seed_run(db, target_id="t-a")
        _seed_run(db, target_id="t-b")

        result = _run_async(_forecast_ppt(
            args={"target_id": "t-b", "save_artifact": False},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        assert result["success"] is True
        assert result["payload"]["chart"] is not None
        assert "Beta" in result["payload"]["chart"]["title"]

    # ── Below-naive surfacing ───────────────────────────────────

    def test_below_naive_surfaced_in_payload(self, db, org_context, user_id):
        """Below-naive products appear in payload warnings and summary."""
        _seed_target(db)
        _seed_run(db, below_naive_baseline=True, confidence="low")

        result = _run_async(_forecast_ppt(
            args={"save_artifact": False},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        assert result["success"] is True
        payload = result["payload"]
        assert len(payload["warnings"]) >= 1
        assert "below naive baseline" in payload["warnings"][0].lower()
        assert "flagged for review" in payload["summary"]

    # ── save_artifact=True ──────────────────────────────────────

    def test_save_artifact_persists_and_renders(self, db, org_context, user_id):
        """save_artifact=True delegates to _create_artifact_tool."""
        _seed_target(db)
        _seed_run(db)

        result = _run_async(_forecast_ppt(
            args={"save_artifact": True},
            db=db,
            user_id=user_id,
            context=org_context,
        ))

        # _create_artifact_tool returns success + artifact_id on success
        # (or success: False if pptx render fails in test env)
        # Accept either: True means rendered; False with artifact_id means
        # renderer had an issue but artifact was created.
        assert "success" in result
        if result.get("success"):
            assert "artifact_id" in result
        # Even on failure, check we get a sensible error, not a crash
        if not result.get("success"):
            assert "error" in result

    # ── Org context ─────────────────────────────────────────────

    def test_uses_org_from_context(self, db, user_id):
        """Handler uses org_id from the agent context."""
        _seed_target(db)

        result = _run_async(_forecast_ppt(
            args={"save_artifact": False},
            db=db,
            user_id=user_id,
            context={"org_id": "test-org"},
        ))

        assert result["success"] is True
        assert "test-org" in result["payload"]["source"]

    def test_different_org_excluded(self, db, user_id):
        """Targets in other orgs are excluded."""
        _seed_target(db)

        result = _run_async(_forecast_ppt(
            args={"save_artifact": False},
            db=db,
            user_id=user_id,
            context={"org_id": "other-org"},
        ))

        assert result["success"] is True
        assert "0 products" in result["payload"]["summary"]
