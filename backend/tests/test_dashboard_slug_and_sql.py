"""Regression tests: dashboard slug auto-uniquify + LLM-SQL hygiene (2026-08-28).

Root cause: on the Sales Performance Dashboard build (conv 5f2c2c39) the
dashboard turn died twice:
  1. create_fullstack_dashboard failed hard because the LLM reused slug
     'sales-performance-dashboard-v2' (already exists) — the turn ended with
     "Sorry, I hit an error while responding".
  2. The data step's NL2SQL emitted Python-style doubled percents
     (DATE_FORMAT(x, '%%Y-%%m')) and pasted TWO SELECTs, producing MySQL 1064s.
"""
import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.db.nl_answer_service import _sanitize_llm_sql
from app.services.dashboard_query import render_widget_sql, validate_widget_sql


# ── LLM SQL hygiene ──────────────────────────────────────────────────────

def test_sanitize_collapses_doubled_percent():
    raw = "SELECT DATE_FORMAT(PLANDATE, '%%Y-%%m') AS m FROM t GROUP BY 1"
    assert _sanitize_llm_sql(raw) == "SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS m FROM t GROUP BY 1"


def test_sanitize_keeps_first_statement():
    raw = (
        "SELECT DATE(PLANDATE) AS d, SUM(AMOUNT) AS a FROM t GROUP BY 1;\n\n"
        "SELECT DATE_FORMAT(PLANDATE, '%%Y-%%m') AS m, SUM(AMOUNT) AS a FROM t GROUP BY 1"
    )
    out = _sanitize_llm_sql(raw)
    assert out.startswith("SELECT DATE(PLANDATE)")
    assert "DATE_FORMAT" not in out


def test_sanitize_leaves_plain_sql_untouched():
    sql = "SELECT name, SUM(amount) AS total FROM sales WHERE region = 'E%' GROUP BY name"
    assert _sanitize_llm_sql(sql) == sql


def test_sanitize_empty():
    assert _sanitize_llm_sql("") == ""
    assert _sanitize_llm_sql(None) is None


# ── Dashboard render normalization ───────────────────────────────────────

def test_render_widget_sql_normalizes_doubled_percent():
    sql = "SELECT DATE_FORMAT(PLANDATE, '%%Y-%%m') AS m, SUM(v) AS v FROM t GROUP BY 1"
    rendered = render_widget_sql(sql, params={})
    assert "%%" not in rendered
    assert "DATE_FORMAT(PLANDATE, '%Y-%m')" in rendered
    # Normalized SQL must still pass the read-only/single-statement guard.
    validate_widget_sql(rendered)


def test_validate_widget_sql_still_rejects_multi_statement():
    multi = "SELECT 1;\nSELECT 2"
    try:
        validate_widget_sql(multi)
        raised = False
    except ValueError:
        raised = True
    assert raised is True


# ── Slug auto-uniquify ───────────────────────────────────────────────────

class _FakeDb:
    """db.query(DashboardApp).filter(slug==X).first() returns a row for
    'sales-perf' and None for every suffixed slug."""

    def __init__(self):
        self._last_val = None

    def query(self, model):
        outer = self

        class _Q:
            def filter(self, *a, **kw):
                try:
                    outer._last_val = a[0].right.value
                except Exception:
                    outer._last_val = None
                return self

            def first(self):
                if outer._last_val == "sales-perf":
                    return MagicMock(slug="sales-perf")
                return None

        return _Q()

    def commit(self):
        self.committed = True


def test_create_fullstack_dashboard_auto_uniquifies_taken_slug():
    from app.services.tool_handlers import dashboard_tools

    args = {
        "name": "Sales Performance Dashboard",
        "slug": "sales-perf",
        "description": "test",
        "datasource_id": "kb-1",
        "metrics": [
            {
                "id": "m1", "type": "kpi",
                "title": "Revenue",
                "sql": "SELECT SUM(amount) AS total FROM sales",
                "options": {},
            }
        ],
        "refresh_interval_seconds": 30,
        "theme": "light",
        "style": "standard",
        "scope": "personal",
        "insights": [],
        "layout": [],
    }
    db = _FakeDb()
    ctx = {"org_id": "org-1", "project_id": "proj-1", "conversation_id": "conv-1"}
    mgr = MagicMock()
    mgr.set_status = MagicMock()
    mgr.mount = MagicMock()
    mgr.commit_version = MagicMock()
    mgr.create_app_record = MagicMock(return_value=MagicMock())

    async def _run():
        with (
            patch.object(dashboard_tools, "_user_can_access_kb", return_value=MagicMock(id="kb-1")),
            patch("app.services.dashboard_app.manager.dashboard_app_manager", mgr),
            patch.object(dashboard_tools, "_stamp_dashboard_conversation"),
        ):
            generator = MagicMock()
            generator.app_dir.return_value = _tmp_app_dir()
            with patch("app.services.dashboard_app.generator.get_generator", return_value=generator):
                # enrich_spec best-effort: return spec unchanged.
                with patch(
                    "app.services.dashboard_app.analytics.enrich_spec",
                    side_effect=lambda db, kb, spec: spec,
                ):
                    with patch(
                        "app.services.artifacts.artifact_service.ArtifactService"
                    ) as _ArtSvc:
                        _ArtSvc.return_value.create_artifact.return_value = None
                        return await dashboard_tools._create_fullstack_dashboard(
                            args, db, "user-1", context=ctx,
                        )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is True
    assert result["dashboard_app"]["slug"] == "sales-perf-2"
    # The manager must have been called with the uniquified slug.
    mgr.create_app_record.assert_called()
    call_kwargs = mgr.create_app_record.call_args
    assert call_kwargs[0][0]["slug"] == "sales-perf-2"


def _tmp_app_dir():
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp(prefix="dash_slug_test_"))
    (d / "config.json").write_text("{}")
    (d / "api.py").write_text("# api")
    dist = d / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    return d
