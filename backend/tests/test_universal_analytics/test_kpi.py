"""Tests for universal_analytics/kpi.py — KPI computation engine."""

import pytest


class TestKPIComputation:
    def test_build_kpi_sql_yoy(self):
        """Build KPI SQL for YoY comparison with time bucket."""
        from app.services.universal_analytics.kpi import build_kpi_sql

        sql = build_kpi_sql(
            table="sales",
            time_column="sale_date",
            measure="revenue",
            db_type="mysql",
            dimensions=["region"],
            kpi_type="yoy",
        )
        assert "sales" in sql
        assert "sale_date" in sql
        assert "revenue" in sql
        assert "region" in sql
        assert "LAG" in sql

    def test_build_kpi_sql_mom(self):
        """Build KPI SQL for MoM comparison."""
        from app.services.universal_analytics.kpi import build_kpi_sql

        sql = build_kpi_sql(
            table="sales",
            time_column="sale_date",
            measure="revenue",
            db_type="mysql",
            dimensions=None,
            kpi_type="mom",
        )
        assert "sales" in sql
        assert "sale_date" in sql
        assert "revenue" in sql
        assert "DATE_TRUNC" in sql

    def test_build_kpi_sql_period_over_period(self):
        """Build KPI SQL for custom period comparison."""
        from app.services.universal_analytics.kpi import build_kpi_sql

        sql = build_kpi_sql(
            table="metrics",
            time_column="dt",
            measure="qty",
            db_type="postgres",
            dimensions=None,
            kpi_type="period",
        )
        assert "metrics" in sql
        assert "dt" in sql
        assert "qty" in sql
        assert "AVG" in sql or "SUM" in sql

    def test_build_kpi_sql_no_dimensions(self):
        """KPI SQL without dimensions should still work."""
        from app.services.universal_analytics.kpi import build_kpi_sql

        sql = build_kpi_sql(
            table="data",
            time_column="ts",
            measure="val",
            db_type="sqlite",
            dimensions=[],
            kpi_type="yoy",
        )
        assert "GROUP BY" in sql
        assert "data" in sql
