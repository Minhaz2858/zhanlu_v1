"""EBOM — Situation Room agent-harness wiring (Experience B backend).

Verifies the thin endpoint reuses the existing Agent Harness data-agent without
building a new orchestration framework. The LLM sub-loop is monkeypatched so the
test is deterministic and quota-independent; the real path reuses ask_data_agent.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest

from app.routers.ontology import situation_investigate
from tests.sandbox_seed import ORG, build_sandbox, create_data_db, make_engine, make_session


def _user():
    return SimpleNamespace(id="u-owner", org_id=ORG, role="org_admin")


def _ctx():
    eng = make_engine()
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.db")
    create_data_db(p)
    s = make_session(eng)
    build_sandbox(s, p)
    return s


@pytest.fixture()
def patched_agent(monkeypatch):
    captured = {}

    async def fake(args, db, user_id, context):
        captured["args"] = args
        captured["context"] = context
        return {
            "success": True,
            "answer": "Model X margin declined due to rising component cost.",
            "rows": [{"model": "X", "margin": 750}],
            "sql": "SELECT SUM(margin) FROM sales_transactions WHERE model_id='v-001'",
            "source_id": context["bound_kb_ids"][0],
        }

    import app.services.tool_handlers.delegation_tools as dt
    monkeypatch.setattr(dt, "_ask_data_agent", fake)
    return captured


def test_investigate_reuses_harness(patched_agent):
    s = _ctx()
    import asyncio
    result = asyncio.run(
        situation_investigate({"question": "Why is Model X margin down?"},
                              org_id=None, db=s, user=_user()))
    assert result["success"] is True
    assert "Model X" in result["answer"]
    # Bound to this org's data sources — not a brand-new framework.
    assert patched_agent["context"]["bound_kb_ids"]
    assert "Why is Model X margin down?" in patched_agent["args"]["question"]


def test_investigate_rejects_empty_question():
    s = _ctx()
    import asyncio
    with pytest.raises(Exception):  # HTTPException 400
        asyncio.run(
            situation_investigate({"question": ""}, org_id=None, db=s, user=_user()))
