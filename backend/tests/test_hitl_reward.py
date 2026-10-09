"""P0-1 HITL reward loop tests — thumbs feedback reaches the RL signal."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401, E402  (registers ALL models)
from app.services.agent_invocations import (  # noqa: E402
    apply_human_feedback,
    record_invocation,
    resolve_invocation_for_feedback,
)
from app.models.trajectory_reward import TrajectoryReward  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _agent_app(db):
    """record_invocation binds rows to the general_assistant app."""
    from app.models.agent_app import AgentApp

    existing = (
        db.query(AgentApp).filter(AgentApp.name == "general_assistant").first()
    )
    if existing is None:
        db.add(AgentApp(
            name="general_assistant", description="d", prompt_identity="x",
            status="published", org_id="default-org", app_id="default-app",
        ))
        db.commit()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mk_invocation(db, conv="conv-hitl-1", content="Hello there.", route="react"):
    return record_invocation(
        db,
        conversation_id=conv,
        invocation_type="conversation",
        status="completed",
        input_message="hi",
        assistant_content=content,
        route=route,
    )


def test_thumbs_down_writes_human_reward(db):
    inv_id = _mk_invocation(db)
    out = apply_human_feedback(db, invocation_id=inv_id, rating=-1, comment="wrong")
    assert out is not None
    row = (
        db.query(TrajectoryReward)
        .filter(
            TrajectoryReward.invocation_id == inv_id,
            TrajectoryReward.source == "human",
        )
        .first()
    )
    assert row is not None
    assert row.reward == -1.0
    assert row.components["origin"] == "human_thumbs"
    # the auto reward row must be untouched
    auto = (
        db.query(TrajectoryReward)
        .filter(
            TrajectoryReward.invocation_id == inv_id,
            TrajectoryReward.source == "auto",
        )
        .first()
    )
    assert auto is not None and auto.reward == 1.0


def test_thumbs_up_reward_positive_and_upsert_not_duplicate(db):
    inv_id = _mk_invocation(db, conv="conv-hitl-2", route="fsm")
    first = apply_human_feedback(db, invocation_id=inv_id, rating=1)
    second = apply_human_feedback(db, invocation_id=inv_id, rating=1, comment="great")
    assert first == second
    rows = (
        db.query(TrajectoryReward)
        .filter(
            TrajectoryReward.invocation_id == inv_id,
            TrajectoryReward.source == "human",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].reward == 1.0
    assert rows[0].components["comment"] == "great"
    assert rows[0].route == "fsm"


def test_human_reward_feeds_bandit(db):
    from app.services.rl.policy_bandit import get_route_bandit

    route = "hitl-bandit-route"
    inv_id = _mk_invocation(db, conv="conv-hitl-3", route=route)
    before = get_route_bandit()._counts.get(route, 0)
    apply_human_feedback(db, invocation_id=inv_id, rating=1)
    after = get_route_bandit()._counts.get(route, 0)
    assert after == before + 1


def test_resolve_matches_by_content_prefix(db):
    inv_id = _mk_invocation(db, conv="conv-hitl-4", content="The report shows 42 units.")
    got = resolve_invocation_for_feedback(db, "conv-hitl-4", "The report shows 42 units.")
    assert got == inv_id


def test_resolve_falls_back_to_latest_conversation_turn(db):
    _mk_invocation(db, conv="conv-hitl-5", content="Alpha answer")
    _mk_invocation(
        db, conv="conv-hitl-5", content="Beta answer", route="react"
    )
    got = resolve_invocation_for_feedback(db, "conv-hitl-5", "completely different text")
    assert got is not None  # best-effort fallback found the conversation turn


def test_unknown_invocation_returns_none(db):
    assert apply_human_feedback(db, invocation_id="nope", rating=1) is None
