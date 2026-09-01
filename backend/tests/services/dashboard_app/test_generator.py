"""Regression tests for DashboardAppGenerator — Jinja2 template fill + output layout."""
import asyncio
import ast
import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import patch

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


def test_generated_realtime_has_t11_pg_listen_branch(gen):
    """T11: the generated poller must ship the LISTEN/NOTIFY upgrade path and
    still be valid Python.

    The branch is runtime-gated (is_pg_listen_supported()), so it exists in
    every generated app; the fallback interval poll must remain for non-PG
    backends and as the timeout fallback.
    """
    app_dir = gen.generate(SAMPLE_SPEC)
    src = (app_dir / "realtime.py").read_text(encoding="utf-8")
    ast.parse(src)  # must be valid Python
    assert "is_pg_listen_supported()" in src
    assert "pg_listen_channel(SLUG)" in src
    assert "add_listener" in src  # LISTEN via asyncpg
    assert "notified.wait()" in src  # event-driven wake
    assert "TimeoutError" in src  # interval fallback on silent channel
    assert "_interval_poll_loop" in src  # non-PG fallback preserved
    assert "poll_loop" in src  # manager entry point unchanged


# ── T11 behavioral tests: execute the GENERATED poller, not just syntax-check ──


def _render_app(tmp_path: Path) -> Path:
    """Render the realtime.py template and stub the app-local queries module so
    the generated poller can be imported standalone."""
    gen = DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=tmp_path / "apps")
    app_dir = gen.generate(SAMPLE_SPEC)
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "queries.py").write_text(
        "METRICS = []\n"
        "async def run_metric(db, metric_id):\n"
        "    return {'rows': []}\n",
        encoding="utf-8",
    )
    return app_dir


def _load_generated_realtime(app_dir: Path):
    """Import the generated realtime.py under a unique package name so the
    relative ``from .queries import ...`` resolves, then clean up sys.modules."""
    pkg_name = "gen_app_t11_behavior"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(app_dir)]
    sys.modules[pkg_name] = pkg
    mod_name = f"{pkg_name}.realtime"
    spec = importlib.util.spec_from_file_location(mod_name, app_dir / "realtime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(mod_name, None)
        sys.modules.pop(pkg_name, None)
    return mod


@pytest.mark.asyncio
async def test_generated_listen_loop_refreshes_on_notify(tmp_path):
    """T11: the generated poller subscribes to the per-slug PG channel and
    refreshes immediately when a NOTIFY arrives (mock asyncpg, no real DB)."""
    mod = _load_generated_realtime(_render_app(tmp_path))

    refresh_calls: list[int] = []

    async def fake_refresh(mgr, last_hashes):
        refresh_calls.append(1)

    mod._refresh_once = fake_refresh

    class FakeConn:
        def __init__(self):
            self.listeners: dict[str, object] = {}
            self.closed = False

        async def add_listener(self, channel, cb):
            self.listeners[channel] = cb

        async def remove_listener(self, channel, cb):
            self.listeners.pop(channel, None)

        async def close(self):
            self.closed = True

    class FakeAsyncpg:
        def __init__(self):
            self.conn = FakeConn()
            self.dsns: list[str] = []

        async def connect(self, dsn):
            self.dsns.append(dsn)
            return self.conn

    fake_pg = FakeAsyncpg()
    with (
        patch.object(mod, "is_pg_listen_supported", return_value=True),
        patch.object(mod, "pg_async_dsn", return_value="postgresql://u:p@h/db"),
        patch.dict(sys.modules, {"asyncpg": fake_pg}),
    ):
        task = asyncio.create_task(mod.poll_loop())
        try:
            # Wait until the loop is subscribed to the per-dashboard channel.
            for _ in range(40):
                if fake_pg.conn.listeners:
                    break
                await asyncio.sleep(0.05)
            assert fake_pg.dsns == ["postgresql://u:p@h/db"]
            assert "zhanlu_dashboard_sales-overview" in fake_pg.conn.listeners

            # Fire a NOTIFY → the loop must refresh without waiting the interval.
            cb = fake_pg.conn.listeners["zhanlu_dashboard_sales-overview"]
            cb(fake_pg.conn, 0, "wake")
            for _ in range(40):
                if refresh_calls:
                    break
                await asyncio.sleep(0.05)
            assert refresh_calls, "NOTIFY must trigger an immediate refresh"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fake_pg.conn.closed  # listener torn down cleanly on cancel


@pytest.mark.asyncio
async def test_generated_poll_loop_falls_back_to_interval_on_listen_failure(tmp_path):
    """T11: if the LISTEN connection cannot be established (asyncpg.connect
    raises), poll_loop must fall back to interval polling instead of dying."""
    mod = _load_generated_realtime(_render_app(tmp_path))

    fallback_calls: list[int] = []

    async def fake_interval(mgr, last_hashes):
        fallback_calls.append(1)
        while True:  # stay alive until the test cancels the task
            await asyncio.sleep(3600)

    mod._interval_poll_loop = fake_interval

    class FailAsyncpg:
        async def connect(self, dsn):
            raise RuntimeError("pg down")

    with (
        patch.object(mod, "is_pg_listen_supported", return_value=True),
        patch.dict(sys.modules, {"asyncpg": FailAsyncpg()}),
    ):
        task = asyncio.create_task(mod.poll_loop())
        try:
            for _ in range(40):
                if fallback_calls:
                    break
                await asyncio.sleep(0.05)
            assert fallback_calls, "must fall back to interval polling when LISTEN fails"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
