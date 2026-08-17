"""Tests for business_context — relative-date parser, metric matching,
business-context block builder, and the freshness verdict.

All pure / deterministic helpers; no LLM, no live DB driver.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.knowledge_graph.business_context import (
    build_business_context,
    freshness_verdict,
    parse_relative_window,
)


# ── relative date parser ─────────────────────────────────────────────────────

class TestParseRelativeWindow:
    def _today(self) -> date:
        return date(2026, 8, 15)

    def test_last_n_days(self):
        w = parse_relative_window("supply chain data for last 15 days", self._today())
        assert w == (date(2026, 7, 31), date(2026, 8, 15))

    def test_last_n_days_zh(self):
        w = parse_relative_window("最近 30 天的数据", self._today())
        assert w == (date(2026, 7, 16), date(2026, 8, 15))

    def test_last_n_weeks(self):
        w = parse_relative_window("sales for past 4 weeks", self._today())
        assert w[1] == date(2026, 8, 15)
        assert w[0] == date(2026, 7, 18)

    def test_last_n_months(self):
        w = parse_relative_window("last 3 months", self._today())
        assert w == (date(2026, 5, 15), date(2026, 8, 15))

    def test_this_week(self):
        # 2026-08-15 is a Saturday → Monday is 2026-08-10.
        w = parse_relative_window("this week", self._today())
        assert w == (date(2026, 8, 10), date(2026, 8, 15))

    def test_last_week(self):
        w = parse_relative_window("上周", self._today())
        assert w == (date(2026, 8, 3), date(2026, 8, 9))

    def test_this_month(self):
        w = parse_relative_window("本月", self._today())
        assert w == (date(2026, 8, 1), date(2026, 8, 15))

    def test_last_year(self):
        w = parse_relative_window("last year", self._today())
        assert w == (date(2025, 1, 1), date(2025, 12, 31))

    def test_ytd(self):
        w = parse_relative_window("ytd revenue", self._today())
        assert w == (date(2026, 1, 1), date(2026, 8, 15))

    def test_no_window_returns_none(self):
        assert parse_relative_window("show me total revenue", self._today()) is None

    def test_empty_question_returns_none(self):
        assert parse_relative_window("", self._today()) is None


# ── freshness verdict ────────────────────────────────────────────────────────

@pytest.fixture
def db_with_coverage(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/cov.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _add_table(db, kb_id: str, table_name: str, max_date: str):
    from app.models.knowledge_catalog import KBTableMeta

    db.add(KBTableMeta(
        id=f"{kb_id}-{table_name}",
        kb_id=kb_id,
        table_name=table_name,
        org_id="o", app_id="a",
        coverage_json={"date_column": "ship_date", "min_date": "2025-01-01",
                       "max_date": max_date, "probed_at": "2026-08-15T00:00:00Z"},
    ))
    db.commit()


class TestFreshnessVerdict:
    def test_stale_window_short_circuits(self, db_with_coverage):
        _add_table(db_with_coverage, "kb1", "shipments", "2025-12-31")
        window = (date(2026, 8, 1), date(2026, 8, 15))
        v = freshness_verdict(db_with_coverage, "kb1", window)
        assert v is not None
        assert v["stale"] is True
        assert v["max_date"] == "2025-12-31"
        assert "no data after 2025-12-31" in v["message"]

    def test_non_stale_window_returns_none(self, db_with_coverage):
        _add_table(db_with_coverage, "kb1", "shipments", "2026-08-10")
        window = (date(2026, 8, 1), date(2026, 8, 15))
        # max_date (2026-08-10) is within the window → not stale.
        v = freshness_verdict(db_with_coverage, "kb1", window)
        assert v is None

    def test_no_window_returns_none(self, db_with_coverage):
        _add_table(db_with_coverage, "kb1", "shipments", "2025-12-31")
        assert freshness_verdict(db_with_coverage, "kb1", None) is None

    def test_no_coverage_returns_none(self, db_with_coverage):
        window = (date(2026, 8, 1), date(2026, 8, 15))
        assert freshness_verdict(db_with_coverage, "kb-missing", window) is None


# ── business context injection ───────────────────────────────────────────────

@pytest.fixture
def db_with_metric(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/metric.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _set_flag(value: bool):
    from app.config import settings
    return pytest.MonkeyPatch().setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", value)


class TestBuildBusinessContext:
    def test_flag_off_returns_empty(self, db_with_metric):
        from app.config import settings
        _set_flag(False)
        # No metric rows at all, but flag is off → empty regardless.
        block = build_business_context(db_with_metric, "proj1", "kb1", "gross margin")
        assert block == ""

    def test_matches_approved_metric_by_alias(self, db_with_metric, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", True)
        from app.models.knowledge_catalog import ProjectMetric

        db_with_metric.add(ProjectMetric(
            id="m1", project_id="proj1", kb_id="kb1", name="Gross Margin",
            aliases=["毛利率", "gross margin"], definition="revenue - cogs",
            sql_expression="SUM(margin)/SUM(rev)", unit="%",
            default_aggregation="avg", status="approved", source="llm",
        ))
        db_with_metric.commit()

        block = build_business_context(db_with_metric, "proj1", "kb1", "what is the 毛利率?")
        assert "Gross Margin" in block
        assert "gross margin" in block.lower() or "毛利率" in block

    def test_proposed_metric_not_injected(self, db_with_metric, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", True)
        from app.models.knowledge_catalog import ProjectMetric

        db_with_metric.add(ProjectMetric(
            id="m2", project_id="proj1", kb_id="kb1", name="Net Profit",
            aliases=["净利润"], definition="x", sql_expression="y",
            status="proposed", source="llm",
        ))
        db_with_metric.commit()

        block = build_business_context(db_with_metric, "proj1", "kb1", "净利润是多少")
        assert "Net Profit" not in block

    def test_coverage_annotation_included(self, db_with_metric, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", True)
        from app.models.knowledge_catalog import KBTableMeta, ProjectMetric

        db_with_metric.add(KBTableMeta(
            id="kb1-t1", kb_id="kb1", table_name="sales",
            org_id="o", app_id="a",
            coverage_json={"date_column": "order_date", "min_date": "2025-01-01",
                           "max_date": "2026-08-10", "probed_at": "x"},
        ))
        db_with_metric.add(ProjectMetric(
            id="m3", project_id="proj1", kb_id="kb1", name="Revenue",
            aliases=["收入"], definition="sum", sql_expression="SUM(x)",
            status="approved", source="llm",
        ))
        db_with_metric.commit()

        block = build_business_context(db_with_metric, "proj1", "kb1", "收入 trend")
        # Both the matched metric and the coverage annotation should appear.
        assert "Revenue" in block
        assert "sales.order_date" in block
        assert "2026-08-10" in block

    def test_kb_scoping(self, db_with_metric, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", True)
        from app.models.knowledge_catalog import ProjectMetric

        db_with_metric.add(ProjectMetric(
            id="m4", project_id="proj1", kb_id="kb-other", name="Only Other",
            aliases=["only"], definition="d", sql_expression="e",
            status="approved", source="llm",
        ))
        db_with_metric.commit()

        # Question relevant to "only" but metric belongs to a different KB.
        block = build_business_context(db_with_metric, "proj1", "kb1", "only metric")
        assert "Only Other" not in block
