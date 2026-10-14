"""query_database tool tests (Option C Phase 2, 2026-10-14).

The FSM pipeline boxed as a tool: the ReAct orchestrator invokes it and
narrates from grounded rows. The handler must never raise (tool
contract), must cap rows, and must return an honest empty state.
"""
import asyncio
from types import SimpleNamespace

import pytest


def test_tool_registered():
    import app.services.tool_handlers.query_database_tool  # noqa: F401 — triggers registration
    from app.services.tool_registry import registry

    entry = registry.get_entry("query_database")
    assert entry is not None
    assert entry.schema["function"]["name"] == "query_database"


def test_tool_in_data_tool_names():
    """The router/data-intent layer recognizes it as a data tool."""
    from app.services.synexia.data_intent import _DATA_TOOL_NAMES

    assert "query_database" in _DATA_TOOL_NAMES


def _mk_obs(rows, sql="SELECT 1"):
    return SimpleNamespace(success=True, result_data={"rows": rows, "sql": sql})


def test_handler_extracts_grounded_rows(tmp_path, monkeypatch):
    import app.services.tool_handlers.query_database_tool as qd

    class _FakeFSM:
        def __init__(self, db):
            pass

        def run(self, req, *a, **k):
            return SimpleNamespace(observations=[_mk_obs(
                [{"product": "A", "revenue": 100}, {"product": "B", "revenue": 200}],
                sql="SELECT product, SUM(revenue) FROM t GROUP BY product",
            )], verification={"ok": True, "detail": "ok"})

    import app.services.synexia.fsm as fsm_mod

    monkeypatch.setattr(fsm_mod, "SynexiaFSM", _FakeFSM)
    monkeypatch.setattr(
        fsm_mod, "ExecutionRequest", lambda **kw: SimpleNamespace(**kw))

    async def run():
        return await qd._query_database(
            {"question": "top products"}, db=object(), user_id="u1",
            context={"agent_name": "analyst", "org_id": "org-a",
                     "conversation_id": "c1", "endpoint": None},
        )

    out = asyncio.run(run())
    assert out["success"] is True
    assert out["row_count"] == 2
    assert out["rows"][0]["product"] == "A"
    assert out["verified"] is True
    assert "Grounded rows" in out["note"]


def test_handler_caps_rows_at_50(tmp_path, monkeypatch):
    import app.services.tool_handlers.query_database_tool as qd

    rows = [{"i": i} for i in range(200)]

    class _FakeFSM:
        def __init__(self, db):
            pass

        def run(self, req, *a, **k):
            return SimpleNamespace(observations=[_mk_obs(rows)])

    import app.services.synexia.fsm as fsm_mod

    monkeypatch.setattr(fsm_mod, "SynexiaFSM", _FakeFSM)
    monkeypatch.setattr(
        fsm_mod, "ExecutionRequest", lambda **kw: SimpleNamespace(**kw))

    out = asyncio.run(qd._query_database(
        {"question": "everything"}, db=object(), user_id="u1", context={}))
    assert out["row_count"] == 200        # honest total
    assert len(out["rows"]) == 50         # capped payload


def test_handler_honest_empty(tmp_path, monkeypatch):
    import app.services.tool_handlers.query_database_tool as qd

    class _FakeFSM:
        def __init__(self, db):
            pass

        def run(self, req, *a, **k):
            return SimpleNamespace(observations=[_mk_obs([])])

    import app.services.synexia.fsm as fsm_mod

    monkeypatch.setattr(fsm_mod, "SynexiaFSM", _FakeFSM)
    monkeypatch.setattr(
        fsm_mod, "ExecutionRequest", lambda **kw: SimpleNamespace(**kw))

    out = asyncio.run(qd._query_database(
        {"question": "nothing there"}, db=object(), user_id="u1", context={}))
    assert out["success"] is True
    assert out["row_count"] == 0
    assert "never invent" in out["note"]


def test_handler_fails_soft_on_pipeline_crash(tmp_path, monkeypatch):
    import app.services.tool_handlers.query_database_tool as qd

    class _FakeFSM:
        def __init__(self, db):
            raise RuntimeError("pipeline exploded")

    import app.services.synexia.fsm as fsm_mod

    monkeypatch.setattr(fsm_mod, "SynexiaFSM", _FakeFSM)
    monkeypatch.setattr(
        fsm_mod, "ExecutionRequest", lambda **kw: SimpleNamespace(**kw))

    async def run():
        return await qd._query_database(
            {"question": "q"}, db=object(), user_id="u1", context={})

    out = asyncio.run(run())
    assert out["success"] is False
    assert "never invent" in out.get("hint", "")


def test_handler_requires_question():
    import app.services.tool_handlers.query_database_tool as qd

    out = asyncio.run(qd._query_database({}, db=None, user_id=None, context={}))
    assert out["success"] is False
