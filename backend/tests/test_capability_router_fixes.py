"""Regression tests for M1, M4, M5 capability router fixes."""
import os, inspect

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from app.services.synexia.capability_router import (
    _execute_skill_node,
    _topological_sort,
    execute_plan_nodes,
)


# ── M1: skill node context threading ──

def test_execute_skill_node_signature():
    """M1: _execute_skill_node must accept user_id and data_ctx_extras."""
    sig = inspect.signature(_execute_skill_node)
    params = list(sig.parameters.keys())
    assert "user_id" in params
    assert "data_ctx_extras" in params


# ── M4: adaptive revisions passthrough ──

def test_execute_plan_nodes_accepts_adaptive_revisions():
    """M4: execute_plan_nodes signature includes adaptive_revisions."""
    sig = inspect.signature(execute_plan_nodes)
    assert "adaptive_revisions" in sig.parameters


# ── M5: cycle detection ──

class MockNode:
    def __init__(self, seq, deps=None):
        self.seq = seq
        self.dependencies = deps or []


def test_topological_sort_dag_order():
    """M5: DAG produces correct topological order."""
    nodes = [MockNode(1, [2, 3]), MockNode(2, [4]), MockNode(3, [4]), MockNode(4)]
    result = _topological_sort(nodes)
    seqs = [n.seq for n in result]
    assert seqs.index(4) < seqs.index(2) < seqs.index(1)
    assert seqs.index(4) < seqs.index(3) < seqs.index(1)


def test_topological_sort_detects_cycle():
    """M5: raises ValueError on cycle."""
    with pytest.raises(ValueError, match="cycle"):
        _topological_sort([MockNode(1, [2]), MockNode(2, [1])])


def test_topological_sort_detects_self_loop():
    """M5: raises ValueError on self-loop."""
    with pytest.raises(ValueError, match="cycle"):
        _topological_sort([MockNode(1, [1])])
