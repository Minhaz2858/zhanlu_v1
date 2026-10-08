"""NL2SQL planner — OCI agentic planning stage (P1-5) + turn state (P1-6).

Pins: flag-gated (default off), JSON extraction tolerates fences, metric must
come from the vocabulary rule is the prompt's job but the payload is validated,
clarification only opens the gate when BOTH the planner is uncertain AND
NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED, guidance block is bounded, and turn state
persists per (conversation, KB) without ever raising.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.base import TimestampedBase
from app.models.nl2sql_turn_state import Nl2sqlTurnState
from app.services.db import nl2sql_planner


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _plan_json(**overrides):
    base = {
        "intent": "metric",
        "metric": "revenue",
        "metric_definition": "Revenue",
        "entity": "OCI",
        "time_range": {
            "type": "relative",
            "start": "2026-08-01",
            "end": "2026-08-31",
            "label": "last month",
        },
        "slices": ["region"],
        "confidence": 0.9,
        "ambiguous": False,
        "assumptions": ["Interpreted 'did' as revenue."],
        "clarification": None,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_PLANNER_ENABLED", True)


def _mock_llm(monkeypatch, payload: str):
    async def fake_chat(messages, endpoint=None, max_tokens=700):
        return payload

    monkeypatch.setattr(nl2sql_planner, "_chat", fake_chat)


# ── planning ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_question_returns_structured_plan(db, monkeypatch):
    _mock_llm(monkeypatch, _plan_json())
    plan = await nl2sql_planner.plan_question(db, "kb1", "How did OCI do last month?")
    assert plan is not None
    assert plan["metric"] == "revenue"
    assert plan["time_range"]["start"] == "2026-08-01"
    assert plan["needs_clarification"] is False
    assert "<resolved-intent>" in plan["guidance"]
    assert "revenue" in plan["guidance"]


@pytest.mark.asyncio
async def test_flag_off_returns_none(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_PLANNER_ENABLED", False)
    _mock_llm(monkeypatch, _plan_json())
    assert await nl2sql_planner.plan_question(db, "kb1", "q") is None


@pytest.mark.asyncio
async def test_unparseable_output_returns_none(db, monkeypatch):
    _mock_llm(monkeypatch, "sorry, I cannot answer that")
    assert await nl2sql_planner.plan_question(db, "kb1", "q") is None


@pytest.mark.asyncio
async def test_fenced_json_is_parsed(db, monkeypatch):
    _mock_llm(monkeypatch, f"```json\n{_plan_json()}\n```")
    plan = await nl2sql_planner.plan_question(db, "kb1", "q")
    assert plan is not None and plan["metric"] == "revenue"


# ── clarify gate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_gate_opens_when_uncertain_and_flag_on(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", True)
    _mock_llm(
        monkeypatch,
        _plan_json(
            confidence=0.3,
            ambiguous=True,
            clarification={
                "question": "Which metric do you mean by 'did'?",
                "options": [
                    {"label": "Revenue", "meaning": "total sales revenue"},
                    {"label": "Margin", "meaning": "gross margin"},
                ],
            },
        ),
    )
    plan = await nl2sql_planner.plan_question(db, "kb1", "How did OCI do last month?")
    assert plan["ambiguous"] is True
    assert plan["needs_clarification"] is True
    assert len(plan["clarification"]["options"]) == 2


@pytest.mark.asyncio
async def test_clarify_gate_stays_shut_when_flag_off(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", False)
    _mock_llm(
        monkeypatch,
        _plan_json(
            confidence=0.3,
            ambiguous=True,
            clarification={
                "question": "Which metric?",
                "options": [{"label": "Revenue", "meaning": "sales"}],
            },
        ),
    )
    plan = await nl2sql_planner.plan_question(db, "kb1", "q")
    assert plan["ambiguous"] is True
    assert plan["needs_clarification"] is False
    # The defaults path still gets its steering block.
    assert "<resolved-intent>" in plan["guidance"]


@pytest.mark.asyncio
async def test_clarify_gate_stays_shut_when_confident(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", True)
    _mock_llm(
        monkeypatch,
        _plan_json(
            confidence=0.95,
            ambiguous=True,
            clarification={
                "question": "Which metric?",
                "options": [{"label": "Revenue", "meaning": "sales"}],
            },
        ),
    )
    plan = await nl2sql_planner.plan_question(db, "kb1", "q")
    assert plan["needs_clarification"] is False


@pytest.mark.asyncio
async def test_clarification_without_options_is_dropped(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", True)
    _mock_llm(
        monkeypatch,
        _plan_json(
            confidence=0.3,
            ambiguous=True,
            clarification={"question": "Which metric?", "options": []},
        ),
    )
    plan = await nl2sql_planner.plan_question(db, "kb1", "q")
    assert plan["clarification"] is None
    assert plan["needs_clarification"] is False


# ── multi-turn state ──────────────────────────────────────────────────────


def test_turn_state_roundtrip(db):
    assert nl2sql_planner.save_turn_state(
        db,
        "conv1",
        "kb1",
        resolved={"metric": "revenue", "entity": "OCI"},
        defaults={"time_range": {"label": "last month"}},
    ) is True
    state = nl2sql_planner.load_turn_state(db, "conv1", "kb1")
    assert state["resolved"]["metric"] == "revenue"

    # Second turn merges, never duplicates the row.
    nl2sql_planner.save_turn_state(db, "conv1", "kb1", resolved={"scope": "NA"})
    state = nl2sql_planner.load_turn_state(db, "conv1", "kb1")
    assert state["resolved"]["scope"] == "NA"
    assert state["resolved"]["metric"] == "revenue"
    assert db.query(Nl2sqlTurnState).count() == 1


def test_turn_state_rejected_accumulates_and_caps(db):
    nl2sql_planner.save_turn_state(db, "conv1", "kb1", rejected=["margin"])
    nl2sql_planner.save_turn_state(db, "conv1", "kb1", rejected=["margin", "bookings"])
    state = nl2sql_planner.load_turn_state(db, "conv1", "kb1")
    assert state["rejected"] == ["margin", "bookings"]


def test_turn_state_scoped_per_conversation_and_kb(db):
    nl2sql_planner.save_turn_state(db, "conv1", "kb1", resolved={"metric": "revenue"})
    assert nl2sql_planner.load_turn_state(db, "conv2", "kb1") is None
    assert nl2sql_planner.load_turn_state(db, "conv1", "kb2") is None


def test_turn_state_requires_conversation_id(db):
    assert nl2sql_planner.save_turn_state(db, None, "kb1", resolved={"a": 1}) is False
    assert nl2sql_planner.load_turn_state(db, None, "kb1") is None


@pytest.mark.asyncio
async def test_carried_state_flag_set_when_state_exists(db, monkeypatch):
    nl2sql_planner.save_turn_state(db, "conv1", "kb1", resolved={"metric": "revenue"})
    _mock_llm(monkeypatch, _plan_json())
    plan = await nl2sql_planner.plan_question(
        db, "kb1", "same again", conversation_id="conv1"
    )
    assert plan["carried_state"] is True

    plan2 = await nl2sql_planner.plan_question(db, "kb1", "same again")
    assert plan2["carried_state"] is False


# ── helpers ───────────────────────────────────────────────────────────────


def test_build_guidance_is_bounded():
    big = {"guidance": None, "assumptions": ["x" * 500] * 10, "slices": list("abcdefghij")}
    big.update({"intent": "metric", "metric": "m", "entity": "e"})
    block = nl2sql_planner._build_guidance(big)
    assert len(block) <= nl2sql_planner._MAX_GUIDANCE_CHARS


def test_parse_plan_json_extracts_embedded_object():
    text = 'Here you go:\n{"intent": "trend", "metric": null}\nDone.'
    obj = nl2sql_planner._parse_plan_json(text)
    assert obj == {"intent": "trend", "metric": None}
    assert nl2sql_planner._parse_plan_json("no json here") is None
    assert nl2sql_planner._parse_plan_json("") is None
