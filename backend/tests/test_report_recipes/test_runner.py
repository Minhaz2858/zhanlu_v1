"""Report Recipe Runner — execution order, validation, section assembly."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database import Base
from app.models.report_recipe import ReportRecipe
from app.services.report_recipes import runner as rr
from app.services.report_recipes.seed_recipes import SEED_RECIPES


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


class TestSeedRecipesGeneric:
    """Seed recipes must contain NO domain-specific examples."""

    def test_no_domain_terms_in_seeds(self):
        forbidden = ["C5", "C9", "ethylene", "naphtha", "裂解", "乙烯", "石脑油"]
        for recipe in SEED_RECIPES:
            blob = str(recipe)
            for term in forbidden:
                assert term.lower() not in blob.lower(), (
                    f"Seed recipe '{recipe['name']}' contains domain term: {term}"
                )

    def test_five_seed_recipes(self):
        names = {r["name"] for r in SEED_RECIPES}
        assert "sales" in names
        assert "weekly" in names
        assert "monthly" in names
        assert "inventory" in names
        assert "customer" in names

    def test_all_seeds_have_sections(self):
        for r in SEED_RECIPES:
            assert r.get("sections"), f"Recipe '{r['name']}' has no sections"


class TestRunnerSqlBundle:
    def test_executes_sql_bundle_in_order(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test",
            sql_bundle=[
                {"key": "totals", "sql": "SELECT 1 AS val"},
                {"key": "breakdown", "sql": "SELECT 2 AS val"},
            ],
            sections=[
                {"title": "Totals", "source_key": "totals"},
                {"title": "Breakdown", "source_key": "breakdown"},
            ],
            output_format="markdown",
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()

        mock_qs = MagicMock()
        mock_qs.execute.side_effect = [
            {"rows": [{"val": 1}], "row_count": 1},
            {"rows": [{"val": 2}], "row_count": 1},
        ]
        with patch.object(rr, "QueryService", return_value=mock_qs):
            result = rr.run_recipe(db, recipe, kb_id="kb-1")

        assert result["success"] is True
        assert len(result["sections"]) == 2
        assert result["sections"][0]["title"] == "Totals"
        assert result["sections"][0]["data"] == [{"val": 1}]
        # Verify execution order
        assert mock_qs.execute.call_count == 2
        assert mock_qs.execute.call_args_list[0][0][1] == "SELECT 1 AS val"

    def test_empty_results_pass_without_validation_rules(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_empty",
            sql_bundle=[{"key": "q", "sql": "SELECT 1"}],
            sections=[{"title": "Q", "source_key": "q"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {"rows": [], "row_count": 0}
        with patch.object(rr, "QueryService", return_value=mock_qs):
            result = rr.run_recipe(db, recipe, kb_id="kb-1")
        assert result["success"] is True

    def test_validation_non_empty_rule_fails_on_empty(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_val",
            sql_bundle=[{"key": "q", "sql": "SELECT 1"}],
            sections=[{"title": "Q", "source_key": "q"}],
            validation_rules=[{"rule": "non_empty", "source_key": "q"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {"rows": [], "row_count": 0}
        with patch.object(rr, "QueryService", return_value=mock_qs):
            result = rr.run_recipe(db, recipe, kb_id="kb-1")
        assert result["success"] is False
        assert any("non_empty" in v["rule"] for v in result["validation_results"])

    def test_sql_execution_failure_soft_fails(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_fail",
            sql_bundle=[{"key": "q", "sql": "BAD SQL"}],
            sections=[{"title": "Q", "source_key": "q"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()
        mock_qs = MagicMock()
        mock_qs.execute.side_effect = RuntimeError("syntax error")
        with patch.object(rr, "QueryService", return_value=mock_qs):
            result = rr.run_recipe(db, recipe, kb_id="kb-1")
        assert result["success"] is False
        assert "error" in result

    def test_no_sql_bundle_returns_error(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_no_sql",
            sections=[{"title": "Q", "source_key": "q"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()
        result = rr.run_recipe(db, recipe, kb_id="kb-1")
        assert result["success"] is False
        assert "no sql_bundle" in result.get("error", "").lower()


class TestRunnerMetricResolution:
    def test_resolves_metrics_via_metric_definition(self, db):
        from app.models.metric_definition import MetricDefinition

        md = MetricDefinition(
            id=str(uuid.uuid4()), name="total_revenue",
            datasource_id="kb-1", base_sql="SELECT SUM(amount) AS revenue FROM orders",
            aggregation="sum", org_id="default-org", app_id="default-app",
        )
        db.add(md)
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_metrics",
            required_metrics=["total_revenue"],
            sections=[{"title": "Revenue", "source_key": "total_revenue"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()

        mock_qs = MagicMock()
        mock_qs.execute.return_value = {"rows": [{"revenue": 999}], "row_count": 1}
        with patch.object(rr, "QueryService", return_value=mock_qs):
            result = rr.run_recipe(db, recipe, kb_id="kb-1")
        assert result["success"] is True
        assert result["sections"][0]["data"] == [{"revenue": 999}]

    def test_missing_metric_skipped_gracefully(self, db):
        recipe = ReportRecipe(
            id=str(uuid.uuid4()), name="test_missing_metric",
            required_metrics=["nonexistent_metric"],
            sections=[{"title": "X", "source_key": "nonexistent_metric"}],
            org_id="default-org", app_id="default-app",
        )
        db.add(recipe)
        db.commit()
        result = rr.run_recipe(db, recipe, kb_id="kb-1")
        # No SQL to run → soft fail
        assert result["success"] is False
