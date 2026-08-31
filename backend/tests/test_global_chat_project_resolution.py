"""Regression test: global chat (general_assistant, no project selected)
must resolve a project named in the user message and bind its KBs.

Covers:
1. 'make a c5 c9 market view ppt' → resolves C5_C9 project → its KBs bound
2. No project mention → zero bound KBs (anti-leakage preserved)
3. Explicit project selected → normal scoping still works
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.data_source_runtime.data_source_runtime import (
    _resolve_project_from_message,
    prepare_data_source_runtime,
)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_kb(**kw):
    defaults = dict(
        id="kb-x", name="X", description="", source_kind="file", db_type="",
        database_name="", file_type="pdf", file_url="", indexing_status="ready",
        chunk_count=10, project_id=None, project="global", org_id="default-org",
        app_id="default-app", is_deleted=False,
    )
    defaults.update(kw)
    return _Row(**defaults)


def _fake_agent(name="general_assistant", org_id="default-org", app_id="default-app"):
    return _Row(id="agent-1", name=name, org_id=org_id, app_id=app_id,
                knowledge_bases=[], is_deleted=False)


@pytest.fixture
def db():
    kb = _fake_kb(id="kb-market", name="Market Research Data", source_kind="file",
                  project_id="proj-c5c9", project="C5_C9")
    kb2 = _fake_kb(id="kb-warehouse", name="aipdp_data_warehouse_prod",
                   source_kind="database", project_id="proj-c5c9", project="C5_C9")
    kb_demo = _fake_kb(id="kb-demo-001", name="Demo E2E PostgreSQL",
                       source_kind="database", project_id=None, project="global")
    proj_c5 = _Row(id="proj-c5c9", name="C5_C9", is_deleted=False)
    proj_global = _Row(id="proj-global", name="Global", is_deleted=False)

    m = MagicMock()
    m.query.return_value.filter.return_value.all.return_value = [proj_c5, proj_global]

    def _query(model):
        q = MagicMock()
        if model.__name__ == "Project":
            q.filter.return_value.all.return_value = [proj_c5, proj_global]
        return q
    m.query.side_effect = _query
    m.get.side_effect = lambda model, pk: None
    return m


def test_resolve_project_from_message_c5c9(db):
    pid, pname = _resolve_project_from_message(
        db, "make a c5 c9 market view ppt don't use my data use market data",
        "default-org", "default-app",
    )
    assert pid == "proj-c5c9"
    assert pname == "C5_C9"


def test_resolve_project_from_message_underscore(db):
    pid, pname = _resolve_project_from_message(
        db, "show C5_C9 market", "default-org", "default-app",
    )
    assert pid == "proj-c5c9"


def test_resolve_project_no_mention(db):
    pid, pname = _resolve_project_from_message(
        db, "hello, how are you?", "default-org", "default-app",
    )
    assert pid is None
    assert pname is None


def test_resolve_project_empty(db):
    pid, pname = _resolve_project_from_message(db, None, "default-org", "default-app")
    assert pid is None


@patch("app.services.data_source_runtime.data_source_runtime._extend_with_project_kbs")
def test_prepare_global_chat_resolves_project(mock_extend, db):
    """general_assistant + no project + message names project → KBs bound."""
    agent = _fake_agent()
    # simulate _extend_with_project_kbs returning the two KB ids
    mock_extend.return_value = ["kb-market", "kb-warehouse"]

    with patch("app.services.data_source_runtime.data_source_runtime._maybe_extend_with_workspace_auto_bind",
               return_value=[]):
        with patch("app.services.data_source_runtime.data_source_runtime._build_project_context_block",
                   return_value="PROJECT CONTEXT"):
            tools, prompt, extras = prepare_data_source_runtime(
                db, agent, [], "",
                user_message="make a c5 c9 market view ppt don't use my data use market data",
            )

    assert extras.get("bound_kb_ids") == ["kb-market", "kb-warehouse"]
    mock_extend.assert_called_once()
    # project context block injected
    assert "PROJECT CONTEXT" in prompt


@patch("app.services.data_source_runtime.data_source_runtime._extend_with_project_kbs")
def test_prepare_global_chat_no_mention_zeros(mock_extend, db):
    """general_assistant + no project + no project mention → zero KBs."""
    agent = _fake_agent()

    with patch("app.services.data_source_runtime.data_source_runtime._maybe_extend_with_workspace_auto_bind",
               return_value=["kb-stale"]):
        tools, prompt, extras = prepare_data_source_runtime(
            db, agent, [], "", user_message="just say hi",
        )

    assert extras.get("bound_kb_ids") in (None, [])
    assert not tools


def test_prepare_selected_project_normal(db):
    """Explicit project selection still scopes normally."""
    agent = _fake_agent(name="Data Analyst")

    with patch("app.services.data_source_runtime.data_source_runtime._maybe_extend_with_workspace_auto_bind",
               return_value=[]):
        with patch("app.services.data_source_runtime.data_source_runtime._extend_with_project_kbs",
                   return_value=["kb-market"]):
            tools, prompt, extras = prepare_data_source_runtime(
                db, agent, [], "", selected_project_id="proj-c5c9",
                selected_project_name="C5_C9",
                user_message="make a ppt",
            )

    assert extras.get("bound_kb_ids") == ["kb-market"]
