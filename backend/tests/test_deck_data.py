"""Tests for ``deck_data`` — real-row grounding + user context for deck planning.

The deck pipeline historically builds slides from the LLM-authored
``payload.chart.data``.  These tests lock in the recovery of the REAL query
rows the agent fetched during the conversation (nl2sql ObservationRecords on
the Execution, then conversation tool results) and the user/brand context that
should shape the deck's copy.
"""

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_conversation import AgentConversation
from app.models.execution import Execution, ObservationRecord
from app.models.user import User


def _make_db():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _add_execution(db, *, conversation_id="conv-1", user_message="sales ppt"):
    ex = Execution(conversation_id=conversation_id, user_message=user_message)
    db.add(ex)
    db.flush()
    return ex


def _add_obs(
    db, execution, *, seq=0, observation_type="nl2sql",
    result_data=None, success=True,
):
    obs = ObservationRecord(
        execution_id=execution.id,
        seq=seq,
        observation_type=observation_type,
        result_data=result_data or {},
        success=success,
    )
    db.add(obs)
    db.flush()
    return obs


# ── collect_grounded_rows ────────────────────────────────────────────────

def test_collect_reads_observation_data_key():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    rows = [{"month": "Jan", "revenue": 100}, {"month": "Feb", "revenue": 120}]
    _add_obs(db, ex, seq=0, result_data={"sql": "SELECT ...", "data": rows})
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-1")
    assert got == rows


def test_collect_reads_observation_rows_key():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    rows = [{"product": "A", "sales": 10}]
    _add_obs(db, ex, seq=0, result_data={"rows": rows})
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-1")
    assert got == rows


def test_collect_reads_nested_result():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    rows = [{"region": "EA", "tons": 5}]
    _add_obs(db, ex, seq=0, result_data={"result": {"data": rows}})
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-1")
    assert got == rows


def test_collect_skips_failed_observations():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    good = [{"month": "Jan", "revenue": 100}]
    _add_obs(db, ex, seq=0, result_data={"data": good})
    _add_obs(db, ex, seq=1, result_data={"data": [{"x": 1}]}, success=False)
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-1")
    assert got == good


def test_collect_prefers_observation_with_most_rows():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    thin = [{"a": 1}]
    rich = [{"b": 1}, {"b": 2}, {"b": 3}]
    _add_obs(db, ex, seq=0, result_data={"data": thin})
    _add_obs(db, ex, seq=1, result_data={"data": rich})
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-1")
    assert got == rich


def test_collect_falls_back_to_conversation_mining():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    # No executions/observations — only conversation history tool results.
    conv = AgentConversation(
        id="conv-7",
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_data_agent",
                        "results": {"rows": [{"region": "N", "value": 3}]},
                    }
                ],
            }
        ],
    )
    db.add(conv)
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-7")
    assert got == [{"region": "N", "value": 3}]


def test_collect_empty_when_nothing_found():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    db.add(AgentConversation(id="conv-empty", messages=[]))
    db.commit()

    got = collect_grounded_rows(db, conversation_id="conv-empty")
    assert got == []


def test_collect_by_execution_id():
    from app.services.artifacts.deck_data import collect_grounded_rows

    db = _make_db()
    ex = _add_execution(db)
    rows = [{"month": "Mar", "sales": 42}]
    _add_obs(db, ex, seq=0, result_data={"data": rows})
    # A second execution with its own data must NOT leak in.
    ex2 = _add_execution(db, conversation_id="conv-other")
    _add_obs(db, ex2, seq=0, result_data={"data": [{"other": True}]})
    db.commit()

    got = collect_grounded_rows(db, execution_id=ex.id)
    assert got == rows


# ── build_deck_user_context ──────────────────────────────────────────────

def test_user_context_includes_brand_tokens():
    from app.services.artifacts.deck_data import build_deck_user_context

    db = _make_db()
    db.add(
        User(
            email="a@b.c", full_name="Ana",
            password_hash="x", role_descriptions=["CFO"],
        )
    )
    db.flush()
    # Brand kit lives in workspace_settings.
    from app.models.workspace_settings import WorkspaceSetting
    db.add(
        WorkspaceSetting(
            key="brand_kit", org_id="default-org", app_id="default-app",
            value=json.dumps({
                "name": "Acme",
                "colors": {"primary": "#7c3aed"},
                "fonts": {"heading": "Inter"},
            }),
        )
    )
    db.commit()

    ctx = build_deck_user_context(db, user_id="u1")
    assert ctx is not None
    assert ctx["brand_name"] == "Acme"
    tokens = ctx["brand_tokens"]
    assert tokens["primary"] == "#7c3aed"
    assert tokens["fonts"] == {"heading": "Inter"}


def test_user_context_role_when_flag_on(monkeypatch):
    from app.config import settings
    from app.services.artifacts.deck_data import build_deck_user_context

    monkeypatch.setattr(settings, "ROLE_PERSONALIZATION_ENABLED", True)
    db = _make_db()
    u = User(
        email="r@b.c", full_name="R",
        password_hash="x",
        role_descriptions=["Financial Analyst"],
        role_description_text="R runs weekly P&L reporting.",
    )
    db.add(u)
    db.flush()
    db.commit()

    ctx = build_deck_user_context(db, user_id=u.id)
    assert ctx is not None
    assert "Financial Analyst" in ctx["role_text"]
    assert "P&L" in ctx["role_text"]


def test_user_context_none_when_empty():
    from app.services.artifacts.deck_data import build_deck_user_context

    db = _make_db()
    db.commit()

    ctx = build_deck_user_context(db, user_id="missing")
    assert ctx is None
