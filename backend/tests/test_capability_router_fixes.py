"""Regression tests for M1, M4, M5 capability router fixes."""
import os, inspect
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from app.services.synexia.capability_router import (
    _execute_skill_node,
    _execute_synthesize_node,
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


def test_execute_skill_node_dispatches_load_skill_body_with_exact_id():
    """Skill nodes must call load_skill_body with exact skill_id/name."""
    execution = SimpleNamespace(id="exec-1", conversation_id="conv-1", app_id="app-1", agent_name="general_assistant")
    node = SimpleNamespace(id="node-1", name="Load selected skill: weekly-sales-report", inputs={"skill_id": "tool-123", "skill_name": "weekly-sales-report"})

    with patch("app.services.agent_tools.execute_tool_with_retry") as mock_exec, patch(
        "app.services.synexia.capability_router._record_observation"
    ) as mock_record:
        mock_exec.return_value = {"success": True, "name": "weekly-sales-report", "body": "# Skill"}
        mock_record.return_value = SimpleNamespace(success=True)

        _execute_skill_node(db=None, execution=execution, node=node, user_id="user-1")

    assert mock_exec.call_count == 1
    args, kwargs = mock_exec.call_args
    assert args[0] == "load_skill_body"
    assert args[1]["skill_id"] == "tool-123"
    assert args[1]["name"] == "weekly-sales-report"


def test_execute_synthesize_node_forwards_latest_skill_methodology(monkeypatch):
    """Synthesize should receive the latest loaded skill body so output follows the selected skill."""
    import app.services.synexia.capability_router as cr
    from app.services.synexia.contracts import FinalizeResult, ReportCardPayload

    execution = SimpleNamespace(
        id="exec-2",
        user_message="make weekly sales report",
        task_spec={"entities": {"source_name": "sales_db", "source_id": "kb-1"}},
    )
    node = SimpleNamespace(name="Write report summary", inputs={})
    captured = {}

    monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [])
    monkeypatch.setattr(
        cr,
        "_get_latest_skill_observation",
        lambda db, ex: SimpleNamespace(
            result_data={"name": "weekly-sales-report", "body": "# Weekly Sales Report\n\n## Structure\nUse KPI-first summary."},
            request_args={"name": "weekly-sales-report"},
        ),
    )

    async def fake_synthesize(**kwargs):
        captured.update(kwargs)
        return FinalizeResult(
            task_kind="report",
            assistant_content="summary",
            report_card_payload=ReportCardPayload(title="T", summary="S"),
            user_signal="default",
        )

    monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
    monkeypatch.setattr(cr, "_record_observation", lambda db, ex, node, **kw: SimpleNamespace(**kw))

    _execute_synthesize_node(MagicMock(), execution, node)

    assert captured["skill_name"] == "weekly-sales-report"
    assert "Use KPI-first summary" in captured["skill_methodology"]


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


# ── Deck grounding: tool context carries execution_id ──

def test_execute_tool_node_context_includes_execution_id():
    """Tool nodes must surface ``execution_id`` so the artifact path can
    ground a deck in the REAL query rows of that execution."""
    from unittest.mock import AsyncMock
    from app.services.synexia.capability_router import _execute_tool_node

    execution = SimpleNamespace(
        id="exec-9", conversation_id="conv-1",
        app_id="app-1", agent_name="general_assistant",
    )
    node = SimpleNamespace(id="node-1", name="ask_data_agent", inputs={"query": "sales"})

    with patch(
        "app.services.agent_tools.execute_tool_with_retry", new_callable=AsyncMock
    ) as mock_exec, patch(
        "app.services.synexia.capability_router._record_observation"
    ) as mock_record:
        mock_exec.return_value = {"success": True, "response": "ok"}
        mock_record.return_value = SimpleNamespace(success=True)

        _execute_tool_node(db=None, execution=execution, node=node, user_id="user-1")

    _, kwargs = mock_exec.call_args
    ctx = kwargs["context"]
    assert ctx["execution_id"] == "exec-9"
    assert ctx["conversation_id"] == "conv-1"
