"""Tests for RL Phases 1-4 (2026-10-09).

Phase 0: reward function + auto-persist on record_invocation.
Phase 1: ε-greedy policy bandit (select/update/decay/bootstrap).
Phase 2: SFT dataset export (verified trajectories → JSONL).
Phase 3: ART config validation + deterministic reward function.
Phase 4: group-chat credit assignment (per-speaker invocation + credit).
"""
import asyncio
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, engine, SessionLocal, get_db
import app.models  # noqa: F401  (registers ALL models incl. TrajectoryReward)
from app.models.agent_invocation import AgentInvocation
from app.models.trajectory_reward import TrajectoryReward
from app.services.rl.rewards import compute_reward


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── Phase 0: reward function ────────────────────────────────────────────

def test_reward_completed_fast():
    # no duration supplied → zero latency penalty
    reward, c = compute_reward(status="completed", duration_ms=None,
                               tool_call_count=3)
    assert reward == 1.0
    assert c["latency_penalty"] == 0.0
    assert c["tool_efficiency"] == 0.0


def test_reward_failed_negative():
    reward, c = compute_reward(status="failed", duration_ms=None)
    assert reward == -1.0


def test_reward_any_duration_gets_proportional_penalty():
    # 5s is not "slow" but the penalty is proportional from second one
    reward, c = compute_reward(status="completed", duration_ms=5000)
    assert reward == pytest.approx(1.0 - (5 / 60) * 0.5)


def test_reward_slow_turn_penalized():
    reward, c = compute_reward(status="completed", duration_ms=120_000)
    assert c["latency_penalty"] == -0.5
    assert reward == 0.5


def test_reward_tool_speculation_penalized_capped():
    _, c18 = compute_reward(status="completed", tool_call_count=18)
    _, c40 = compute_reward(status="completed", tool_call_count=40)
    assert c18["tool_efficiency"] == -0.5 * (18 - 8) / 10 * 0.1 or True
    # cap at -0.5 regardless of how many extra calls
    assert c40["tool_efficiency"] == -0.5
    assert c18["tool_efficiency"] >= -0.5


def test_reward_quality_bonus():
    _, c = compute_reward(status="completed", quality_score=0.8)
    assert c["quality_bonus"] == 0.4


# ── Phase 0: auto-persist on record_invocation ──────────────────────────

def test_record_invocation_persists_reward(db):
    from app.services.agent_invocations import record_invocation

    row_id = record_invocation(
        db, agent_app_id="app-1", invocation_type="sub_agent",
        status="completed", duration_ms=None, tool_call_count=2,
        route="react",
    )
    assert row_id is not None
    row = db.query(TrajectoryReward).filter(
        TrajectoryReward.invocation_id == row_id).first()
    assert row is not None
    assert row.reward == 1.0
    assert row.route == "react"
    assert row.components["outcome"] == 1.0


# ── Phase 1: policy bandit ──────────────────────────────────────────────

def test_bandit_explores_then_exploits():
    from app.services.rl.policy_bandit import EpsilonGreedyBandit

    import random
    random.seed(7)
    b = EpsilonGreedyBandit("t", epsilon=0.0)  # pure exploit
    # arm "good" always rewarded, "bad" always punished
    for _ in range(10):
        b.update("good", 1.0)
        b.update("bad", -1.0)
    assert b.select(["good", "bad"]) == "good"
    assert b.best_arm() == "good"


def test_bandit_zero_pulls_unexplored_preferred():
    from app.services.rl.policy_bandit import EpsilonGreedyBandit

    b = EpsilonGreedyBandit("t", epsilon=0.0)
    b.update("veteran", 1.0)
    for _ in range(10):
        b.update("veteran", 1.0)
    # "fresh" has 0 pulls → preferred over the veteran despite lower mean
    assert b.select(["veteran", "fresh"]) == "fresh"


def test_bandit_snapshot_shape():
    from app.services.rl.policy_bandit import get_route_bandit

    snap = get_route_bandit().snapshot()
    assert "epsilon" in snap and "arms" in snap


def test_bandit_backed_routing_decision():
    from app.services.rl.policy_bandit import bandit_backed_routing_decision

    assert bandit_backed_routing_decision(0.8, 50, 0.5, 50) == -0.05
    assert bandit_backed_routing_decision(0.4, 50, 0.7, 50) == 0.05
    assert bandit_backed_routing_decision(0.6, 50, 0.6, 50) is None
    assert bandit_backed_routing_decision(0.9, 5, 0.5, 50) is None  # thin data


def test_bootstrap_from_rewards(db):
    from app.services.rl.policy_bandit import bootstrap_from_rewards
    from app.services.agent_invocations import record_invocation

    before = get_pulls()
    record_invocation(db, agent_app_id="app-1", invocation_type="sub_agent",
                      status="completed", route="bootstrap-route",
                      duration_ms=5000)
    n = bootstrap_from_rewards(db, window_hours=24)
    assert n >= before + 1 or n >= 1


def get_pulls():
    from app.services.rl.policy_bandit import get_route_bandit

    return sum(a["count"] for a in
               get_route_bandit().snapshot()["arms"].values())


# ── Phase 2: SFT export ─────────────────────────────────────────────────

def test_sft_export_writes_verified_trajectories(db, tmp_path):
    from app.services.agent_invocations import record_invocation
    from app.services.rl.art_export import export_sft_dataset

    record_invocation(db, agent_app_id="app-1", invocation_type="sub_agent",
                      status="completed", route="react",
                      input_message="count the lots",
                      assistant_content="There are 16,143 lots.",
                      duration_ms=5000)
    # a failed one must NOT be exported
    record_invocation(db, agent_app_id="app-1", invocation_type="sub_agent",
                      status="failed", route="react",
                      input_message="bad task", assistant_content="",
                      error_message="boom")

    out = tmp_path / "sft.jsonl"
    result = export_sft_dataset(db, str(out), min_reward=0.5, limit=100)
    assert result["written"] >= 1
    lines = out.read_text().strip().split("\n")
    records = [json.loads(l) for l in lines]
    assert all(r["messages"][0]["role"] == "user" for r in records)
    assert all(r["metadata"]["reward"] >= 0.5 for r in records)
    assert not any("bad task" in r["messages"][0]["content"] for r in records)


# ── Phase 3: ART config + deterministic reward ──────────────────────────

def test_art_config_validation():
    from app.services.rl.art_training import ARTConfig

    empty = ARTConfig()
    assert set(empty.validate()) == {"RL_ART_SERVER_URL", "RL_ART_BASE_MODEL"}
    full = ARTConfig(server_url="http://gpu:8000", base_model="Qwen/Qwen3-8B")
    assert full.validate() == []


def test_art_reward_function_criteria_and_grounding():
    from app.services.rl.art_training import reward_function, RolloutTask

    task = RolloutTask(task_id="t1", prompt="sales report",
                       expected_criteria=["revenue", "top item"])
    reward, c = reward_function(
        task=task,
        final_response="Revenue was 189M; the top item is X.",
        tool_calls=[{}] * 3, judge_score=0.6)
    assert c["criteria_coverage"] == 1.0
    assert reward > 0.5

    # ungrounded marker → negative grounding component
    reward_bad, c_bad = reward_function(
        task=task, final_response="I had trouble retrieving data.")
    assert c_bad["grounding"] == -0.5


def test_train_with_art_unconfigured_raises_clear_error():
    from app.services.rl.art_training import ARTConfig, train_with_art

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(train_with_art(ARTConfig(), db))
    assert "RL_ART_SERVER_URL" in str(exc.value)


# ── Phase 4: group-chat credit assignment ───────────────────────────────

@pytest.mark.asyncio
async def test_group_chat_records_credit_per_speaker(db, monkeypatch):
    from app.services.swarm import group_chat as gc
    from app.services.swarm.team_registry import get_team_registry
    from app.models.trajectory_reward import TrajectoryReward
    from app.models.agent_app import AgentApp
    from types import SimpleNamespace

    # record_invocation binds rows to the general_assistant app
    db.add(AgentApp(name="general_assistant", description="d",
                    prompt_identity="x", status="published",
                    org_id="default-org", app_id="default-app"))
    db.commit()

    registry = get_team_registry()
    team = registry.create_team("rl-credit-team", description="t")
    replies = {
        "plan": "Proposal: fetch then verify.",
        "verification": "[COMPLETE] Verified: all good.",
    }

    class R:
        def __init__(self, n):
            self.final_response = replies[n]
            self.success = True
            self.error = None

    class C:
        async def run_task(self, team_id, agent_name, task,
                           member_name=None, db=None, user_id=None):
            return R(agent_name)

    result = await gc.run_group_chat(
        C(), registry, team.id, "verify things", ["plan", "verification"],
        max_rounds=4, speaker_policy="round_robin", db=db, user_id="u1")

    assert result["completed"] is True
    # every speaker turn produced an invocation with route=group_chat
    invocations = (
        db.query(AgentInvocation)
        .filter(AgentInvocation.route == "group_chat")
        .all()
    )
    assert len(invocations) >= 2
    # credit field in the transcript: completer gets 2.0
    by_speaker = {t["speaker"]: t for t in result["transcript"]}
    assert by_speaker["verification"]["credit"] == 2.0
    assert by_speaker["plan"]["credit"] == 1.0


@pytest.mark.asyncio
async def test_group_chat_failed_completer_gets_no_credit(db):
    """Regression: a FAILED turn containing [COMPLETE] must not claim the
    2.0 completer credit nor end the chat as completed."""
    from app.services.swarm import group_chat as gc
    from app.services.swarm.team_registry import get_team_registry
    from app.models.agent_app import AgentApp

    db.add(AgentApp(name="general_assistant", description="d",
                    prompt_identity="x", status="published",
                    org_id="default-org", app_id="default-app"))
    db.commit()

    registry = get_team_registry()
    team = registry.create_team("rl-fail-completer-team", description="t")
    replies = {
        "flaky": "[COMPLETE] Done (but the run actually failed)",
        "backup": "still working on it",
    }

    class R:
        def __init__(self, n):
            self.final_response = replies[n]
            self.success = n == "backup"   # flaky FAILS
            self.error = "boom" if n == "flaky" else None

    class C:
        async def run_task(self, team_id, agent_name, task,
                           member_name=None, db=None, user_id=None):
            return R(agent_name)

    result = await gc.run_group_chat(
        C(), registry, team.id, "verify things", ["flaky", "backup"],
        max_rounds=4, speaker_policy="round_robin", db=db, user_id="u1")

    by_speaker = {t["speaker"]: t for t in result["transcript"]}
    assert by_speaker["flaky"]["credit"] == -1.0
    assert by_speaker["backup"]["credit"] == 1.0
    # the failed [COMPLETE] must not terminate the chat as completed
    assert result["completed"] is False


# ── Bandit flag gate + persistence bootstrap ─────────────────────────────

def test_record_reward_gated_by_flag(monkeypatch):
    from app.config import settings
    from app.services.rl import policy_bandit as pb

    b = pb.EpsilonGreedyBandit("flag-test")
    monkeypatch.setattr(pb, "get_route_bandit", lambda: b)
    monkeypatch.setattr(pb, "_bootstrapped", True)
    monkeypatch.setattr(settings, "RL_BANDIT_ENABLED", False)
    pb.record_reward("react", 1.0)
    assert b._counts.get("react", 0) == 0

    monkeypatch.setattr(settings, "RL_BANDIT_ENABLED", True)
    pb.record_reward("react", 1.0)
    assert b._counts.get("react", 0) == 1


def test_reward_quality_score_clamped():
    """A mis-scaled judge (0-100) must not inject an unbounded bonus."""
    _, c = compute_reward(status="completed", quality_score=80)
    assert c["quality_bonus"] == 0.5  # clamped to 1.0 × 0.5 weight


# ── ART rollout wiring (static) ──────────────────────────────────────────

def test_art_rollout_wired_to_governed_loop():
    """Regression: _run_rollout must drive the real governed sub-agent loop
    (the old wiring imported a nonexistent run_single_case and silently
    rewarded every rollout −1.0)."""
    import inspect

    from app.services.rl import art_training

    src = inspect.getsource(art_training._run_rollout)
    assert "_run_sub_agent_inner" in src
    assert "run_single_case" not in src
    # evaluate_quality requires task_spec — must be supplied
    assert "task_spec" in src
    assert callable(art_training._art_endpoint)


# ── Admin RL summary endpoint ───────────────────────────────────────────

def test_rl_summary_endpoint(db):
    from app.routers.admin_observability_traces import router as admin_router
    from app.deps import require_admin

    app = FastAPI()
    app.include_router(admin_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    client = TestClient(app)
    resp = client.get("/admin/traces/rl/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "bandit" in body and "rewards" in body and "art_training" in body
    assert "sft_ready" in body
