"""Tests for the delegation nudge (P1-4 agent-gaps build)."""

from __future__ import annotations

from app.services.delegation_nudge import (
    delegation_nudge_directive,
    parallelizable_ask_count,
)


def test_top_n_lists_fire():
    msg = "List the top 5 customers by revenue, top 5 products by volume, and top 3 regions by margin."
    assert parallelizable_ask_count(msg) >= 2
    d = delegation_nudge_directive(msg)
    assert d is not None
    assert "delegate_task" in d


def test_numbered_enumeration_fires():
    msg = "Do three things: 1) analyze revenue, 2) check inventory, 3) list suppliers."
    assert parallelizable_ask_count(msg) >= 3
    assert delegation_nudge_directive(msg) is not None


def test_zh_enumeration_fires():
    msg = "分别分析华东、华南、华北三个区域的销售情况"
    assert parallelizable_ask_count(msg) >= 2
    assert delegation_nudge_directive(msg) is not None


def test_single_ask_no_nudge():
    msg = "What were total sales last month?"
    assert parallelizable_ask_count(msg) == 0
    assert delegation_nudge_directive(msg) is None


def test_dashboard_turn_no_nudge():
    msg = "Build a dashboard with top 5 customers, top 5 products, and top 3 regions."
    assert delegation_nudge_directive(msg) is None


def test_empty_no_nudge():
    assert delegation_nudge_directive(None) is None
    assert delegation_nudge_directive("") is None


def test_two_items_is_threshold():
    # 2 clauses = score 2 → fires (>= 2)
    msg = "top 5 customers and top 3 products"
    assert delegation_nudge_directive(msg) is not None
