"""Regression: the full-stack dashboard app metric endpoint must be ASYNC.

T17 global catch-all route ``app_metric`` (routers/dashboards.py) used to be a
sync endpoint that normalized the async ``run_metric`` coroutine with
``asyncio.get_event_loop().run_until_complete(...)``. FastAPI runs sync
endpoints in a threadpool (AnyIO worker) thread, which has NO running event
loop — so every widget request 500'd with
``RuntimeError: There is no current event loop in thread 'AnyIO worker thread'``
and the dashboard frontend rendered "No data" on all cards + "Reconnecting…".

The route is now ``async def`` and awaits ``run_metric`` directly, and it
injects the SQLAlchemy ``db`` session (the old version passed ``db=None``,
which ``QueryService`` needs to resolve the datasource KB).

These tests pin BOTH regressions: coroutine-function (runs on the event loop,
never a threadpool) and db propagation.
"""
import asyncio
import inspect
import types

from fastapi import HTTPException

from app.routers import dashboards as dash_router


class _FakeManager:
    def get_app(self, slug):  # noqa: ARG002
        return object()  # not None → app record exists


class _FakeReq:
    query_params = {}


async def _fake_run_metric(db, metric_id, filters):
    # The OLD route passed db=None here; QueryService(db)._load_kb would then
    # crash. db must arrive intact from the Depends(get_db) injection.
    assert db is not None, "run_metric must receive the SQLAlchemy session"
    return {"columns": ["v"], "rows": [{"v": 42}], "error": None, "truncated": False}


def test_app_metric_is_async_coroutine_function() -> None:
    """FastAPI must run this on the event loop, not in a threadpool.

    A sync endpoint here is the exact regression that 500'd every widget.
    """
    assert inspect.iscoroutinefunction(dash_router.app_metric)


def test_app_metric_awaits_and_passes_db(monkeypatch) -> None:
    mod = types.SimpleNamespace(run_metric=_fake_run_metric)
    monkeypatch.setattr(dash_router, "_resolve_module", lambda slug: mod)
    monkeypatch.setattr(dash_router, "dashboard_app_manager", _FakeManager())

    result = asyncio.run(
        dash_router.app_metric("demo-slug", "kpi_x", _FakeReq(), db=object())
    )
    assert result["metric_id"] == "kpi_x"
    assert result["data"]["rows"] == [{"v": 42}]
    assert result["data"]["error"] is None


def test_app_metric_surfaces_query_error_as_404(monkeypatch) -> None:
    async def _boom(db, metric_id, filters):  # noqa: ARG001
        raise RuntimeError("metric exploded")

    mod = types.SimpleNamespace(run_metric=_boom)
    monkeypatch.setattr(dash_router, "_resolve_module", lambda slug: mod)
    monkeypatch.setattr(dash_router, "dashboard_app_manager", _FakeManager())

    try:
        asyncio.run(dash_router.app_metric("demo-slug", "kpi_x", _FakeReq(), db=object()))
    except HTTPException as exc:
        assert exc.status_code == 404
        assert "metric not found" in str(exc.detail)
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException(404) for a failing metric")


def test_app_metric_404_when_module_missing(monkeypatch) -> None:
    def _missing(slug):  # noqa: ARG001
        raise ImportError("no app")

    monkeypatch.setattr(dash_router, "_resolve_module", _missing)
    try:
        asyncio.run(dash_router.app_metric("nope", "kpi_x", _FakeReq(), db=object()))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected HTTPException(404) for a missing app")
