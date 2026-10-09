"""P2-6 utility-based plan scoring tests."""
from types import SimpleNamespace as NS

from app.services.synexia.plan_utility import score_plan


def _node(seq, name, node_type="tool", deps=None, description=""):
    return NS(seq=seq, node_type=node_type, name=name,
              description=description, dependencies=deps or [])


def test_empty_plan_scores_none():
    plan = NS(nodes=[])
    out = score_plan(plan)
    assert out["utility"] is None


def test_small_readonly_plan_scores_high():
    plan = NS(nodes=[
        _node(1, "describe_schema", "nl2sql"),
        _node(2, "execute_query", "tool", deps=[1]),
        _node(3, "synthesize report", "synthesize", deps=[2]),
    ])
    out = score_plan(plan)
    assert out["utility"] is not None
    assert out["utility"] >= 0.7
    assert out["components"]["depth"] == 3
    assert out["warnings"] == []


def test_write_heavy_deep_plan_scores_low_with_warnings():
    nodes = [_node(1, "delete rows from ledger", "tool")]
    # chain of 6 sequential write steps
    for i in range(2, 8):
        nodes.append(_node(i, f"update ledger step {i}", "tool", deps=[i - 1]))
    plan = NS(nodes=nodes)
    out = score_plan(plan)
    assert out["utility"] is not None
    assert out["utility"] < 0.5
    assert any("write" in w for w in out["warnings"])
    assert out["components"]["depth"] >= 6


def test_depth_handles_string_node_ids():
    nodes = [
        NS(seq=None, node_id="a", node_type="tool", name="q",
           description="", dependencies=[]),
        NS(seq=None, node_id="b", node_type="tool", name="r",
           description="", dependencies=["a"]),
        NS(seq=None, node_id="c", node_type="tool", name="s",
           description="", dependencies=["b"]),
    ]
    out = score_plan(NS(nodes=nodes))
    assert out["components"]["depth"] == 3


def test_scoring_never_raises_on_garbage():
    out = score_plan(NS(nodes=[NS(seq=1, node_type=None, name=None,
                                   description=None, dependencies=None)]))
    assert "utility" in out
