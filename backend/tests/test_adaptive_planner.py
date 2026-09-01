"""Tests for Phase 3c: adaptive mid-execution re-planning (opt-in).

decide_adaptive_revision reviews accumulated observations after a checkpoint
node and decides proceed|insert_nodes|modify_remaining|complete_early.
Fail-safe: any error or unparseable LLM reply → proceed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.synexia.adaptive_planner import (
    decide_adaptive_revision, AdaptiveDecision,
)


def _obs(tool="nl2sql", success=True, text="rows: 5"):
    return SimpleNamespace(observation_type=tool, success=success,
                           tool_name=tool, result_text=text,
                           result_data={}, error_message=None)


class TestAdaptiveDecision:
    def test_proceed_when_llm_says_proceed(self):
        async def fake_llm(system, messages):
            return {"content": '{"action":"proceed","nodes":[]}'}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"
        assert d.nodes == []

    def test_complete_early_short_circuits(self):
        async def fake_llm(system, messages):
            return {"content": '{"action":"complete_early","nodes":[]}'}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "complete_early"

    def test_insert_nodes_returns_new_steps(self):
        new_nodes = [{"node_type": "nl2sql", "name": "Refine query", "dependencies": []}]
        async def fake_llm(system, messages):
            return {"content": json.dumps({"action": "insert_nodes", "nodes": new_nodes})}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "insert_nodes"
        assert any(n["name"] == "Refine query" for n in d.nodes)

    def test_unparseable_reply_falls_back_to_proceed(self):
        async def fake_llm(system, messages):
            return {"content": "not json at all"}
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[{"name": "synthesize"}], call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"

    def test_llm_exception_falls_back_to_proceed(self):
        async def boom(system, messages):
            raise RuntimeError("llm down")
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[{"name": "synthesize"}], call_llm_fn=boom,
        )
        assert d.action == "proceed"

    def test_no_remaining_nodes_skips_call(self):
        called = {"n": 0}

        async def fake_llm(system, messages):
            called["n"] += 1
            return {"content": '{"action":"proceed"}'}
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[], call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"
        assert called["n"] == 0  # nothing left to revise → skip the LLM call

    def test_bad_action_falls_back_to_proceed(self):
        async def fake_llm(system, messages):
            return {"content": '{"action":"nuke_everything","nodes":[]}'}
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[{"name": "synthesize"}], call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"
