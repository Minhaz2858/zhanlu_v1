"""T8: Deep-linked filtered views — generated template wiring + filter safety.

The template ``api.py.jinja2`` reads every URL query-string key and forwards it
to ``run_metric``; ``queries.py.jinja2`` only substitutes DECLARED ``:dim_*``
tokens and SQL-escapes every value (via ``render_widget_sql``'s ``_literal``).
These tests verify the GENERATED code wires that contract correctly and that
query-string input can never become a column name or break out of the literal.
"""
import importlib.util
import shutil
from pathlib import Path

import pytest

from app.services.dashboard_app.generator import DashboardAppGenerator, TEMPLATE_DIR
from app.services.dashboard_query import render_widget_sql

FILTER_SPEC = {
    "name": "Filtered Sales",
    "slug": "filtered-sales",
    "description": None,
    "datasource_id": "kb-123",
    "design_system_ref": None,
    "refresh_interval_seconds": 15,
    "theme": "light",
    "metrics": [
        {
            "id": "sales_by_product",
            "type": "table",
            "title": "Sales by product",
            "sql": "SELECT FNAME, sum(amount) AS v FROM erp_sales WHERE :dim_product GROUP BY FNAME",
            "options": {"filters": [{"key": "product", "column": "FNAME"}]},
        },
        {
            "id": "plain_kpi",
            "type": "kpi",
            "title": "Total",
            "sql": "SELECT sum(amount) AS v FROM erp_sales",
            "options": {"value_column": "v"},
        },
    ],
}


@pytest.fixture
def gen(tmp_path):
    apps_dir = tmp_path / "apps"
    g = DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=apps_dir)
    yield g
    shutil.rmtree(apps_dir, ignore_errors=True)


def _load_generated_queries(gen, spec=FILTER_SPEC):
    """Render + exec the generated queries.py as a standalone module."""
    app_dir = gen.generate(spec)
    queries_path = app_dir / "queries.py"
    spec_ = importlib.util.spec_from_file_location(
        f"gen_queries_{abs(hash(str(queries_path)))}", queries_path
    )
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


# ── template wiring (source-level) ──


def test_generated_queries_declares_filter_helpers(gen):
    app_dir = gen.generate(FILTER_SPEC)
    src = (app_dir / "queries.py").read_text(encoding="utf-8")
    assert "def metric_dimensions" in src
    assert "def run_metric" in src
    assert "filters: dict | None = None" in src
    # The declared filter (token+column) must land in the generated METRICS spec.
    assert '"filters"' in src and '"FNAME"' in src


def test_generated_api_forwards_query_params(gen):
    app_dir = gen.generate(FILTER_SPEC)
    src = (app_dir / "api.py").read_text(encoding="utf-8")
    assert "request.query_params" in src
    assert "run_metric(db, metric_id, filters)" in src
    # Request must be imported so the endpoint signature compiles.
    assert "Request" in src


# ── generated module behavior ──


def test_metric_dimensions_whitelist(gen):
    mod = _load_generated_queries(gen)
    dims = mod.metric_dimensions(mod.METRICS[0])
    assert dims == [{"token": "product", "column": "FNAME"}]
    # Metric without declared filters exposes no dimensions.
    assert mod.metric_dimensions(mod.METRICS[1]) == []


@pytest.mark.asyncio
async def test_run_metric_forwards_filters_to_sql_runner(gen, monkeypatch):
    mod = _load_generated_queries(gen)
    captured = {}

    async def fake_run(db, kb_id, sql, params, dimensions, max_rows, timeout_s):
        captured["params"] = params
        captured["dims"] = dimensions
        return {"columns": [], "rows": [], "error": None, "truncated": False}

    monkeypatch.setattr(mod, "_run_single_sql", fake_run)
    await mod.run_metric(None, "sales_by_product", {"product": "乙二醇"})
    assert captured["params"] == {"filters": {"product": "乙二醇"}}
    assert captured["dims"] == [{"token": "product", "column": "FNAME"}]


@pytest.mark.asyncio
async def test_run_metric_renders_declared_filter_literal(gen, monkeypatch):
    mod = _load_generated_queries(gen)
    captured = {}

    async def fake_run(db, kb_id, sql, params, dimensions, max_rows, timeout_s):
        captured["rendered"] = render_widget_sql(sql, params, dimensions)
        return {"columns": [], "rows": [], "error": None, "truncated": False}

    monkeypatch.setattr(mod, "_run_single_sql", fake_run)
    await mod.run_metric(None, "sales_by_product", {"product": "乙二醇"})
    assert "FNAME = '乙二醇'" in captured["rendered"]
    # Unset filter (empty value) becomes a no-op predicate, never a crash.
    await mod.run_metric(None, "sales_by_product", {"product": ""})
    assert "1=1" in captured["rendered"]


# ── injection resistance ──


def test_sql_injection_value_stays_inside_literal():
    rendered = render_widget_sql(
        "SELECT FNAME, sum(amount) AS v FROM erp_sales WHERE :dim_product GROUP BY FNAME",
        {"filters": {"product": "乙二醇' OR '1'='1 --"}},
        [{"token": "product", "column": "FNAME"}],
    )
    # Single quotes doubled: the payload cannot break out of the literal.
    assert "FNAME = '乙二醇'' OR ''1''=''1 --'" in rendered


def test_undeclared_filter_key_cannot_become_sql():
    # A query-string key that was never declared in options.filters must never
    # surface as a column name or value in the rendered SQL.
    rendered = render_widget_sql(
        "SELECT FNAME, sum(amount) AS v FROM erp_sales WHERE :dim_product GROUP BY FNAME",
        {"filters": {"product": "乙二醇", "hacker": "x'; DROP TABLE t; --"}},
        [{"token": "product", "column": "FNAME"}],
    )
    assert "FNAME = '乙二醇'" in rendered
    assert "hacker" not in rendered
    assert "DROP TABLE" not in rendered


def test_unknown_dim_token_in_generated_sql_raises(gen):
    # An agent-authored metric that uses :dim_<x> without declaring it must be
    # rejected before execution — never silently ignored.
    with pytest.raises(ValueError, match="Unknown dimension token"):
        render_widget_sql(
            "SELECT * FROM t WHERE :dim_ghost", {"filters": {"ghost": "x"}}, []
        )
