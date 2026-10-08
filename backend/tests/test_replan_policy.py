"""A failed data-pipeline node can now trigger a re-plan.

The failure this covers (2026-09-18, live): "Present the contract execution
status for last month using charts" ended with

    (1054, "Unknown column 'state' in 'field list'")

and an essay asking the user to confirm the real column name. Two recovery
loops existed and BOTH excluded the node that failed, by construction:

* ``fsm._should_verify_replan``      — re-planned only ``node_type in ("tool", "skill")``
* ``capability_router._recoverable_failures`` — same, plus "data pipeline is
  never re-planned here"

An ``nl2sql`` node failing on a wrong identifier therefore went straight to
FINALIZE. These tests pin the shared policy: structural data-pipeline failures
are eligible, environmental ones are not, and both loops read the same source.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.synexia.capability_router import _recoverable_failures
from app.services.synexia.replan_policy import (
    collect_replannable_failures,
    is_non_actionable_error,
    is_replannable_failure,
)


def _node(name, node_type, status="failed", error=None):
    return SimpleNamespace(name=name, node_type=node_type, status=status, error=error)


def _plan(*nodes):
    return SimpleNamespace(nodes=list(nodes))


UNKNOWN_COLUMN = '(1054, "Unknown column \'state\' in \'field list\'")'
MISSING_TABLE = "(1146, \"Table 'erp.t_crm_contract' doesn't exist\")"


# ---------------------------------------------------------------------------
# Node-type eligibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "node_type,expected",
    [
        ("tool", True),
        ("skill", True),
        ("nl2sql", True),          # the regression this file exists for
        ("synthesize", False),     # replays the failed input; a new plan adds nothing
        ("sandbox", False),
        ("plan", False),
        (None, False),
    ],
)
def test_node_type_eligibility(node_type, expected):
    assert is_replannable_failure(node_type, UNKNOWN_COLUMN) is expected


# ---------------------------------------------------------------------------
# Error-class eligibility: re-planning an environmental failure only burns budget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error,expected_replannable",
    [
        (UNKNOWN_COLUMN, True),
        (MISSING_TABLE, True),
        ("column \"state\" does not exist", True),
        ("(1064, \"You have an error in your SQL syntax near 'FROM'\")", True),
        ("Policy denied: SQL write blocked", False),
        ("(1045, \"Access denied for user 'ro'@'host'\")", False),
        ("Can't connect to MySQL server on '10.0.0.1'", False),
        ("Query timed out after 30 seconds", False),
        ("(1205, 'Lock wait timeout exceeded')", False),
        ("Permission denied for table erp_t_crm_contract", False),
        ("", True),                # unknown cause stays retryable
    ],
)
def test_error_class_eligibility(error, expected_replannable):
    assert is_non_actionable_error(error) is (not expected_replannable)


# ---------------------------------------------------------------------------
# Failure-context collection — what the planner actually receives
# ---------------------------------------------------------------------------

def test_collect_returns_only_failed_replannable_nodes():
    nodes = [
        _node("query contracts", "nl2sql", error=UNKNOWN_COLUMN),
        _node("fetch market price", "tool", error="upstream 502"),
        _node("summarize", "synthesize", error="input missing"),
        _node("write file", "tool", status="completed", error=None),
        _node("drop table", "tool", error="Policy denied: DDL blocked"),
        _node("reconnect", "tool", error="Can't connect to MySQL server"),
    ]
    ctx = collect_replannable_failures(nodes)
    assert [c["name"] for c in ctx] == ["query contracts", "fetch market price"]
    assert ctx[0] == {
        "name": "query contracts",
        "node_type": "nl2sql",
        "error": UNKNOWN_COLUMN,
    }


def test_collect_never_returns_an_empty_error_for_the_planner():
    ctx = collect_replannable_failures([_node("query", "nl2sql", error=None)])
    assert ctx == [{"name": "query", "node_type": "nl2sql", "error": "unknown error"}]


# ---------------------------------------------------------------------------
# The ACT corrective sub-plan now sees the nl2sql node
# ---------------------------------------------------------------------------

def test_act_corrective_replan_includes_nl2sql():
    plan = _plan(
        _node("query contracts", "nl2sql", error=UNKNOWN_COLUMN),
        _node("summarize", "synthesize", error="no rows"),
    )
    recovered = _recoverable_failures(plan)
    assert [r["node_type"] for r in recovered] == ["nl2sql"]


def test_act_corrective_replan_skips_environmental_nl2sql_failure():
    plan = _plan(_node("query", "nl2sql", error="Query timed out after 30 seconds"))
    assert _recoverable_failures(plan) == []


def test_act_corrective_replan_handles_no_plan():
    assert _recoverable_failures(None) == []
    assert _recoverable_failures(_plan()) == []


# ---------------------------------------------------------------------------
# The FSM's VERIFY-driven re-plan reads the same policy (no drift possible)
# ---------------------------------------------------------------------------

def test_verify_replan_fires_on_nl2sql_failure():
    from app.services.synexia.fsm import SynexiaFSM

    stub = SimpleNamespace(
        plan=_plan(_node("query contracts", "nl2sql", error=UNKNOWN_COLUMN)),
        _last_verify_result=SimpleNamespace(critical_failed=True),
    )
    assert SynexiaFSM._should_verify_replan(stub) is True
    ctx = SynexiaFSM._build_verify_replan_context(stub)
    assert [c["node_type"] for c in ctx] == ["nl2sql"]


def test_verify_replan_requires_a_critical_verify_failure():
    from app.services.synexia.fsm import SynexiaFSM

    stub = SimpleNamespace(
        plan=_plan(_node("query contracts", "nl2sql", error=UNKNOWN_COLUMN)),
        _last_verify_result=SimpleNamespace(critical_failed=False),
    )
    assert SynexiaFSM._should_verify_replan(stub) is False


def test_verify_replan_ignores_environmental_failure_and_no_plan():
    from app.services.synexia.fsm import SynexiaFSM

    stub = SimpleNamespace(
        plan=_plan(_node("query", "nl2sql", error="Access denied for user 'ro'")),
        _last_verify_result=SimpleNamespace(critical_failed=True),
    )
    assert SynexiaFSM._should_verify_replan(stub) is False
    assert SynexiaFSM._build_verify_replan_context(stub) == []

    empty = SimpleNamespace(plan=None, _last_verify_result=SimpleNamespace(critical_failed=True))
    assert SynexiaFSM._should_verify_replan(empty) is False
    assert SynexiaFSM._build_verify_replan_context(empty) == []
