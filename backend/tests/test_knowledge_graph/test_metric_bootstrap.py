"""Tests for metric_bootstrap — LLM proposes rows as 'proposed', NEVER
auto-approved, and never overwrites existing user-curated metrics.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from app.database import Base
from app.models.knowledge_catalog import ProjectMetric


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"mb_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


async def _fake_call_llm(messages, **kwargs):
    """Return a fake LLM response: two metrics."""
    return {
        "data": [
            {"name": "Gross Margin", "aliases": ["毛利率"],
             "definition": "revenue minus cogs",
             "sql_expression": "SUM(pnl)/SUM(rev)", "query_pattern": "margin",
             "unit": "%", "default_aggregation": "avg",
             "bindings": [{"table": "financials", "measure_columns": ["pnl", "rev"],
                           "date_column": "period", "dimensions": []}]},
            {"name": "Revenue", "aliases": ["收入"], "definition": "sum of sales",
             "sql_expression": "SUM(rev)", "query_pattern": "revenue", "unit": "",
             "default_aggregation": "sum",
             "bindings": [{"table": "financials", "measure_columns": ["rev"],
                           "date_column": "period", "dimensions": []}]},
        ]
    }


class TestMetricBootstrap:
    def test_bootstrap_creates_proposed_rows_only(self, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", True)
        with patch("app.services.llm_service.call_llm", _fake_call_llm):
            from app.services.knowledge_graph.metric_bootstrap import (
                bootstrap_project_metrics,
            )
            created = _run(bootstrap_project_metrics(
                db, "proj1", "kb1",
                [{"table_name": "financials", "columns": [
                    {"column_name": "rev", "data_type": "DECIMAL"},
                ]}],
            ))
        assert len(created) == 2
        rows = db.query(ProjectMetric).filter(ProjectMetric.project_id == "proj1").all()
        assert len(rows) == 2
        for r in rows:
            assert r.status == "proposed", "bootstrap must never auto-approve"
            assert r.source == "llm"
            assert r.kb_id == "kb1"

    def test_bootstrap_skips_when_flag_off(self, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", False)
        with patch("app.services.llm_service.call_llm") as mock_llm:
            from app.services.knowledge_graph.metric_bootstrap import (
                bootstrap_project_metrics,
            )
            created = _run(bootstrap_project_metrics(db, "proj1", "kb1", []))
        assert created == []
        mock_llm.assert_not_called()

    def test_bootstrap_replaces_only_proposed(self, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", True)
        # Pre-existing user-approved metric → must not be overwritten.
        db.add(ProjectMetric(
            id="existing", project_id="proj1", kb_id="kb1", name="Gross Margin",
            aliases=["毛利率"], definition="user def", sql_expression="USER_SQL",
            status="approved", source="user",
        ))
        db.commit()
        with patch("app.services.llm_service.call_llm", _fake_call_llm):
            from app.services.knowledge_graph.metric_bootstrap import (
                bootstrap_project_metrics,
            )
            _run(bootstrap_project_metrics(
                db, "proj1", "kb1",
                [{"table_name": "financials", "columns": []}],
            ))
        rows = db.query(ProjectMetric).filter(
            ProjectMetric.project_id == "proj1",
        ).all()
        # The approved user metric must remain AND a proposed Revenue is added.
        assert len(rows) == 2
        approved = [r for r in rows if r.status == "approved"]
        assert approved and approved[0].sql_expression == "USER_SQL"
        # The proposed Revenue metric was added by the bootstrap.
        assert any(r.name == "Revenue" and r.status == "proposed" for r in rows)

    def test_bootstrap_handles_llm_error(self, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", True)

        def boom(*a, **k):
            raise RuntimeError("llm down")

        with patch("app.services.llm_service.call_llm", boom):
            from app.services.knowledge_graph.metric_bootstrap import (
                bootstrap_project_metrics,
            )
            created = _run(bootstrap_project_metrics(db, "proj1", "kb1", []))
        # Error must be swallowed, return empty, not raise.
        assert created == []

    def test_metric_to_dict_shape(self, db):
        pm = ProjectMetric(
            id="m1", project_id="proj1", kb_id="kb1", name="Revenue",
            aliases=["收入"], definition="d", sql_expression="SUM(x)",
            query_pattern="q", unit="", default_aggregation="sum",
            bindings=[{"table": "t"}], status="approved", source="user",
            created_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        from app.services.knowledge_graph.metric_bootstrap import _metric_to_dict
        d = _metric_to_dict(pm)
        assert d["id"] == "m1"
        assert d["name"] == "Revenue"
        assert d["aliases"] == ["收入"]
        assert d["status"] == "approved"
        assert d["created_date"].startswith("2026-01-01T00:00:00")
        assert "bindings" in d
