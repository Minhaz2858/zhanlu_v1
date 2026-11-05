"""Root-cause audit D1/D2/D3 (2026-10-31) — deterministic regression tests.

D1: build_agent_tree was conversation-scoped with no turn filter → every
    assistant bubble rendered ALL prior turns' supervisor rows ("two
    general_assistant — Completed" for two real turns); orphan sub_agent rows
    (span_id=None) hoisted to roots rendered specialists as SIBLINGS of the
    supervisor instead of children.
D2: direct-execution turns recorded no evidence (delegation-only pipeline).
D3: the verify gate failed verification on ANY failed tool even when the
    answer was grounded in successful data calls.

Run: pytest tests/test_execution_tree_turn_scoping.py tests/test_turn_evidence.py -q
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_invocation import AgentInvocation

_T0 = datetime(2026, 10, 31, 10, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    AgentInvocation.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _ga_row(turn_start, conv="conv-1", mins=1.0):
    return AgentInvocation(
        agent_app_id="ga-app",
        conversation_id=conv,
        user_id="u1",
        invocation_type="conversation",
        status="completed",
        output_json={"agent_name": "general_assistant"},
        started_at=turn_start,
        completed_at=turn_start + timedelta(minutes=mins),
    )


def _sub_row(started, name_app="spec-app", conv="conv-1", mins=0.5):
    return AgentInvocation(
        agent_app_id=name_app,
        conversation_id=conv,
        user_id="u1",
        invocation_type="sub_agent",
        status="completed",
        started_at=started,
        completed_at=started + timedelta(minutes=mins),
    )


def _seed_two_turns(db):
    # Turn A: GA turn with one delegation; Turn B: direct GA turn.
    db.add(_ga_row(_T0))
    db.add(_sub_row(_T0 + timedelta(seconds=30)))
    db.add(_ga_row(_T0 + timedelta(minutes=5)))
    db.commit()


# ── D1: turn scoping ────────────────────────────────────────────────────────
def test_started_after_filters_prior_turns(db):
    from app.services.agent_invocations import build_agent_tree

    _seed_two_turns(db)
    tree = build_agent_tree(
        db, conversation_id="conv-1", user_id="u1",
        started_after=_T0 + timedelta(minutes=4, seconds=59),
    )
    assert len(tree["roots"]) == 1
    assert tree["roots"][0]["started_at"].startswith("2026-10-31T10:05")


def test_without_anchor_returns_all_turns(db):
    from app.services.agent_invocations import build_agent_tree

    _seed_two_turns(db)
    tree = build_agent_tree(db, conversation_id="conv-1", user_id="u1")
    assert len(tree["roots"]) == 2


# ── D1: orphan specialist rows attach to the enclosing supervisor turn ─────
def test_orphan_specialist_attaches_to_enclosing_turn(db):
    from app.services.agent_invocations import build_agent_tree

    _seed_two_turns(db)
    tree = build_agent_tree(
        db, conversation_id="conv-1", user_id="u1",
        started_after=_T0 - timedelta(seconds=1),
    )
    assert len(tree["roots"]) == 2  # two supervisors
    turn_a = tree["roots"][0]
    assert turn_a["role"] == "supervisor"
    assert len(turn_a["children"]) == 1
    assert turn_a["children"][0]["role"] == "specialist"
    # Summary counts children too (2 rows total for turn A + 1 for turn B).
    assert tree["summary"]["total"] == 3


def test_orphan_before_any_supervisor_stays_root(db):
    from app.services.agent_invocations import build_agent_tree

    db.add(_sub_row(_T0 - timedelta(minutes=1)))  # delegation BEFORE any turn
    db.add(_ga_row(_T0))
    db.commit()
    tree = build_agent_tree(db, conversation_id="conv-1", user_id="u1")
    assert len(tree["roots"]) == 2


def test_summary_counts_match_rendered_roots(db):
    from app.services.agent_invocations import build_agent_tree, _summarize

    _seed_two_turns(db)
    tree = build_agent_tree(
        db, conversation_id="conv-1", user_id="u1",
        started_after=_T0 + timedelta(minutes=4, seconds=59),
    )
    assert tree["summary"] == _summarize(tree["roots"])
