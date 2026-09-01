"""Regression: general_assistant must not access any data sources when
no project is selected (Ungrouped chat). This prevents cross-project
data leakage even if stale knowledge_bases or workspace auto-bind
settings are present in the database.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.data_source_runtime.data_source_runtime import (
    prepare_data_source_runtime,
    _extend_with_project_kbs,
)


def _make_agent(name="general_assistant", knowledge_bases=None):
    agent = MagicMock()
    agent.name = name
    agent.org_id = "default-org"
    agent.app_id = "default-app"
    agent.knowledge_bases = knowledge_bases or []
    agent.data_bindings = []
    return agent


def test_general_assistant_no_project_returns_empty_bindings():
    """Even if general_assistant has knowledge_bases in the DB and
    auto_bind_all_datasources is enabled, it must return zero bound
    data sources when no project is selected."""
    db = MagicMock()

    # Simulate a workspace with auto_bind_all_datasources = True
    ws = MagicMock()
    ws.value = "true"
    db.query.return_value.filter.return_value.first.return_value = ws

    # Simulate general_assistant with a bound knowledge_base in the DB
    agent = _make_agent(name="general_assistant", knowledge_bases=["kb-123"])

    tools, prompt, extras = prepare_data_source_runtime(
        db,
        agent,
        base_tools=[],
        base_system_prompt="You are a helpful assistant.",
        selected_project_id=None,
        selected_project_name=None,
    )

    assert extras == {} or extras.get("bound_kb_ids") == [], (
        f"general_assistant must have no bound_kb_ids when project_id is None; got: {extras}"
    )
    assert "ask_data_agent" not in [t.get("function", {}).get("name") for t in tools], (
        "ask_data_agent must NOT be injected when no project is selected"
    )


def test_general_assistant_with_project_inherits_project_kbs():
    """When a project IS selected, general_assistant should inherit the
    project's data sources via _extend_with_project_kbs (normal behaviour)."""
    db = MagicMock()

    # Project knowledge base row
    kb = MagicMock()
    kb.id = "kb-proj-1"
    # The query chain is: query().filter().order_by().all()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [kb]

    agent = _make_agent(name="general_assistant", knowledge_bases=[])

    bound_ids = _extend_with_project_kbs(
        db, agent, bound_ids=[],
        project_id="proj-1", project_name="Ecisco BI",
    )

    assert "kb-proj-1" in bound_ids, (
        f"general_assistant should inherit project KBs when project is selected; got: {bound_ids}"
    )
