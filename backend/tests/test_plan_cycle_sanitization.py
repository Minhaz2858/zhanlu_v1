"""Plan dependency sanitization — cycles must not kill the turn (2026-09-22).

Measured live: the LLM planner emitted a cyclic dependency graph for a
follow-up question ("FSM execution failed: Dependency cycle") — the plan was
authored, then the WHOLE turn failed because the executor refused to run it.
A plan with a cycle is salvageable: drop the minimal back-edge set, keep the
nodes. The executor must never see a graph it has to refuse.

Pins: valid DAGs are preserved (order kept); forward deps that merely need
reordering are reordered, not dropped; self-dependencies are dropped; true
cycles are broken by removing the minimal set of back edges; every sanitized
plan passes _validate_acyclic.
"""

from __future__ import annotations

from app.services.synexia.plan_dag import (
    _sanitize_plan_dependencies,
    _validate_acyclic,
)


def test_valid_plan_unchanged():
    steps = [
        {"node_type": "nl2sql", "dependencies": []},
        {"node_type": "synthesize", "dependencies": [0]},
        {"node_type": "sandbox", "dependencies": [1]},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert [s["dependencies"] for s in out] == [[], [0], [1]]
    assert dropped == []
    assert _validate_acyclic(out)


def test_forward_dep_is_reordered_not_dropped():
    """A step that depends on a LATER step is legal in a DAG — the planner
    just wrote the steps out of order. Reorder, never drop."""
    steps = [
        {"node_type": "synthesize", "dependencies": [1]},
        {"node_type": "nl2sql", "dependencies": []},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert dropped == []
    assert _validate_acyclic(out)
    nl2sql = next(s for s in out if s["node_type"] == "nl2sql")
    synth = next(s for s in out if s["node_type"] == "synthesize")
    assert synth["dependencies"] == [out.index(nl2sql)]


def test_self_dependency_dropped():
    steps = [
        {"node_type": "tool", "dependencies": []},
        {"node_type": "tool", "dependencies": [0, 1]},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert out[1]["dependencies"] == [0]
    assert dropped
    assert _validate_acyclic(out)


def test_true_cycle_is_broken_minimally():
    steps = [
        {"node_type": "nl2sql", "dependencies": [2]},
        {"node_type": "synthesize", "dependencies": [0]},
        {"node_type": "sandbox", "dependencies": [1]},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert dropped, "at least one back edge must be removed"
    assert _validate_acyclic(out)
    assert len(out) == 3, "nodes are never removed"


def test_three_node_cycle_broken():
    steps = [
        {"node_type": "a", "dependencies": [1]},
        {"node_type": "b", "dependencies": [2]},
        {"node_type": "c", "dependencies": [0]},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert _validate_acyclic(out)
    assert len(out) == 3


def test_garbage_deps_dropped():
    steps = [
        {"node_type": "tool", "dependencies": []},
        {"node_type": "tool", "dependencies": [99, -1, "x", 0, 1]},
    ]
    out, dropped = _sanitize_plan_dependencies(steps)
    assert out[1]["dependencies"] == [0]
    assert dropped
    assert _validate_acyclic(out)
