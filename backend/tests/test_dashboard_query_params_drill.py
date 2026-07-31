"""run_dashboard_query params + /query endpoint body + backward compatibility.

Covers: token rendering through the query path, None/empty params == current
behavior, drill branch returns __drill__, and the REST endpoint accepts an
optional JSON body (empty or absent) without breaking existing callers.
"""
import asyncio
import app.models  # noqa: F401  -- registers models on Base for the router tests
from types import SimpleNamespace
import pytest


class _FakeQueryService:
    """Captures the rendered SQL it receives instead of hitting a DB."""

    def __init__(self, db):
        self.db = db

    def execute(self, kb_id, sql, max_rows=1000, timeout_s=10):
        return {"rows": [{"rendered": sql}], "truncated": False}


@pytest.fixture
def patched_qs(monkeypatch):
    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _FakeQueryService)
    return dq


def _dash(widgets):
    return SimpleNamespace(id="d1", datasource_kb_id="kb1", definition={"widgets": widgets})


def test_run_query_renders_date_tokens(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT * FROM t WHERE created_at >= :from"}])
    out = asyncio.run(patched_qs.run_dashboard_query(
        None, dash, {"from": "2026-07-01T00:00:00", "to": "2026-07-29T00:00:00"}))
    assert "'2026-07-01 00:00:00'" in out["results"]["w1"]["rows"][0]["rendered"]


def test_run_query_none_params_backward_compat(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT 1 AS n"}])
    out = asyncio.run(patched_qs.run_dashboard_query(None, dash, None))
    assert out["results"]["w1"]["error"] is None
    # No tokens in the SQL → rendered == original (backward compat)
    assert out["results"]["w1"]["rows"][0]["rendered"] == "SELECT 1 AS n"


def test_run_query_renders_dim_filter(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT * FROM t WHERE :dim_region",
                   "options": {"dimensions": [{"token": "region", "column": "region"}]}}])
    out = asyncio.run(patched_qs.run_dashboard_query(
        None, dash, {"filters": {"region": "Asia"}}))
    assert "region = 'Asia'" in out["results"]["w1"]["rows"][0]["rendered"]


def test_run_query_drill_branch_returns_drill_result(patched_qs):
    dash = _dash([{"id": "w1", "sql": "SELECT cat, n FROM t",
                   "options": {"drill": {"value_column": "cat",
                                         "sql": "SELECT p, n FROM t WHERE cat = :drill_value"}}}])
    out = asyncio.run(patched_qs.run_dashboard_query(
        None, dash, {"drill": {"widget_id": "w1", "value": "Books"}}))
    assert "__drill__" in out["results"]
    assert out["results"]["__drill__"]["source_widget_id"] == "w1"
    assert out["results"]["__drill__"]["drill_value"] == "Books"
    assert "Books" in out["results"]["__drill__"]["rows"][0]["rendered"]


def _make_app_and_dash(tmp_path):
    from fastapi import FastAPI
    from app.routers.dashboards import router
    from app.database import get_db, Base
    from app.deps import get_current_user_required
    from app.models.dashboard import Dashboard
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    d = Dashboard(name="D", datasource_kb_id="kb1",
                  definition={"widgets": [{"id": "w1", "sql": "SELECT 1 AS n"}]},
                  org_id="o1", app_id="a1")
    db.add(d)
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(
        id="u1", org_id="o1", app_id="a1")
    return app, d.id


def test_router_query_with_empty_body(patched_qs, tmp_path):
    from fastapi.testclient import TestClient
    app, did = _make_app_and_dash(tmp_path)
    c = TestClient(app)
    r = c.post(f"/api/dashboards/{did}/query", json={})
    assert r.status_code == 200
    assert r.json()["results"]["w1"]["error"] is None


def test_router_query_no_body_backward_compat(patched_qs, tmp_path):
    from fastapi.testclient import TestClient
    app, did = _make_app_and_dash(tmp_path)
    c = TestClient(app)
    r = c.post(f"/api/dashboards/{did}/query")  # no body, no content-type
    assert r.status_code == 200


def test_router_query_passes_drill_param(patched_qs, tmp_path):
    from fastapi.testclient import TestClient
    from app.models.dashboard import Dashboard
    from app.database import get_db, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(f"sqlite:///{tmp_path}/t3.db")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    d = Dashboard(name="D", datasource_kb_id="kb1",
                  definition={"widgets": [{"id": "w1", "sql": "SELECT cat, n FROM t",
                   "options": {"drill": {"value_column": "cat",
                                         "sql": "SELECT p, n FROM t WHERE cat = :drill_value"}}}]},
                  org_id="o1", app_id="a1")
    db.add(d)
    db.commit()
    from fastapi import FastAPI
    from app.routers.dashboards import router
    from app.deps import get_current_user_required
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(
        id="u1", org_id="o1", app_id="a1")
    c = TestClient(app)
    r = c.post(f"/api/dashboards/{d.id}/query",
               json={"drill": {"widget_id": "w1", "value": "Books"}})
    assert r.status_code == 200
    assert "__drill__" in r.json()["results"]


# --- Task 3: create-time drill validation -----------------------------------

def test_widget_spec_rejects_drill_without_sql():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WidgetSpec(id="w1", type="bar", title="T", sql="SELECT 1 AS n",
                   options={"drill": {"value_column": "c"}})


def test_widget_spec_rejects_non_readonly_drill_sql():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WidgetSpec(id="w1", type="bar", title="T", sql="SELECT 1 AS n",
                   options={"drill": {"value_column": "c", "sql": "DELETE FROM t"}})


def test_widget_spec_accepts_valid_drill():
    from app.services.tool_handlers.dashboard_tools import WidgetSpec
    w = WidgetSpec(id="w1", type="bar", title="T", sql="SELECT cat, n FROM t",
                   options={"drill": {"value_column": "cat",
                                      "sql": "SELECT * FROM t WHERE cat = :drill_value"}})
    assert w.options["drill"]["value_column"] == "cat"


def test_router_validates_drill_sql(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.dashboards import router
    from app.database import get_db, Base
    from app.deps import get_current_user_required
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(f"sqlite:///{tmp_path}/t4.db")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(
        id="u1", org_id="o1", app_id="a1")
    c = TestClient(app)
    r = c.post("/api/dashboards", json={
        "name": "D", "datasource_kb_id": "kb1",
        "definition": {"widgets": [{"id": "w1", "type": "bar", "title": "T", "sql": "SELECT 1 AS n",
                     "options": {"drill": {"value_column": "c", "sql": "DELETE FROM t"}}}]}})
    assert r.status_code == 400
    assert "drill" in r.text.lower()
