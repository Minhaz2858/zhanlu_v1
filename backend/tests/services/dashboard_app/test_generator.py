"""Regression tests for DashboardAppGenerator — Jinja2 template fill + output layout."""
import ast
import json
import shutil
from pathlib import Path

import pytest

from app.services.dashboard_app.generator import DashboardAppGenerator, TEMPLATE_DIR

SAMPLE_SPEC = {
    "name": "Sales Overview",
    "slug": "sales-overview",
    "description": "Daily sales KPIs",
    "datasource_id": "kb-123",
    "design_system_ref": "design-system/MASTER.md",
    "metrics": [
        {"id": "kpi_revenue", "type": "kpi", "title": "Revenue", "sql": "SELECT sum(amount) AS revenue FROM sales", "options": {"value_column": "revenue"}},
        {"id": "line_trend", "type": "line", "title": "Trend", "sql": "SELECT d, v FROM trend", "options": {"x_column": "d", "y_column": "v"}},
    ],
    "refresh_interval_seconds": 15,
    "theme": "dark",
}


@pytest.fixture
def gen(tmp_path):
    apps_dir = tmp_path / "apps"
    g = DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=apps_dir)
    yield g
    shutil.rmtree(apps_dir, ignore_errors=True)


def test_generate_creates_app_files(gen):
    app_dir = gen.generate(SAMPLE_SPEC)
    assert app_dir.exists()
    for f in ("api.py", "queries.py", "realtime.py", "__init__.py", "config.json"):
        assert (app_dir / f).is_file(), f"missing {f}"


def test_generate_writes_config_json(gen):
    app_dir = gen.generate(SAMPLE_SPEC)
    cfg = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["slug"] == "sales-overview"
    assert cfg["theme"] == "dark"
    assert cfg["refresh_interval_seconds"] == 15
    assert len(cfg["metrics"]) == 2
    # Raw SQL must NOT leak into config.json
    assert "sql" not in json.dumps(cfg)


def test_generate_api_does_not_contain_raw_sql(gen):
    app_dir = gen.generate(SAMPLE_SPEC)
    api_src = (app_dir / "api.py").read_text(encoding="utf-8")
    # The raw user SQL must NOT leak into the public API module / config — it
    # lives only in queries.py (executed through the guarded path). Frontend
    # display hints (e.g. options.value_column) are fine; the literal SELECT is not.
    assert "SELECT sum(amount)" not in api_src
    assert "FROM sales" not in api_src


def test_generate_idempotent_config(gen):
    d1 = gen.generate(SAMPLE_SPEC)
    c1 = (d1 / "config.json").read_text(encoding="utf-8")
    d2 = gen.generate(SAMPLE_SPEC)
    c2 = (d2 / "config.json").read_text(encoding="utf-8")
    assert c1 == c2


def test_generated_api_handles_none_and_bool_values(gen):
    """Regression: tojson emits JSON null/true/false which is NOT valid Python.

    Specs with description/design_system_ref absent (None) or bool options used
    to produce `NameError: name 'null' is not defined` at import time, 404ing
    the whole dashboard. The topython filter must emit valid Python literals.
    """
    spec = dict(SAMPLE_SPEC)
    spec.pop("description", None)
    spec.pop("design_system_ref", None)
    spec["metrics"] = [
        {
            "id": "kpi_x", "type": "kpi", "title": "X", "sql": "SELECT 1 AS v",
            "options": {"value_column": "v", "show_sparkline": True, "format": None},
        },
    ]
    app_dir = gen.generate(spec)

    api_src = (app_dir / "api.py").read_text(encoding="utf-8")
    queries_src = (app_dir / "queries.py").read_text(encoding="utf-8")
    for src in (api_src, queries_src):
        assert "null" not in src and "true" not in src and "false" not in src, (
            "generated Python source leaked JSON literals"
        )

    # Both generated modules must be valid Python (syntax check). Real import
    # through the mount path is covered by test_static_serving.py.
    for src in (api_src, queries_src):
        ast.parse(src)
