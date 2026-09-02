"""Tests: workflow_builder decision-summary persistence (Task 4).

Covers ``save_flow_from_decision_summary`` + ``resolve_entity_references``
in app.services.workflow_runtime — the backend branch that turns a
workflow_builder ``:::decision-summary`` (action == "save_flow") into a
persisted draft workflow.

Router-direct pattern (no TestClient): StaticPool SQLite session,
SimpleNamespace user — mirrors tests/routers/test_workflows_router.py.
"""

import asyncio
import json
import sys
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.agent_conversation import AgentConversation  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.mcp_server import McpServer  # noqa: E402
from app.models.tool import Tool  # noqa: E402
from app.routers import agents  # noqa: E402
from app.services import workflow_runtime as wfr  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _user(uid="user-1", role="user"):
    return SimpleNamespace(id=uid, role=role)


def _mk_workflow(db, wf_id="wf-1", owner="user-1"):
    wf = AgentApp(
        id=wf_id,
        name="Order Summary",
        description="",
        flow_mode=True,
        flow={"version": 1, "nodes": [], "edges": []},
        status="draft",
        app_id="default-app",
        org_id="default-org",
        created_by_id=owner,
    )
    db.add(wf)
    db.commit()
    return wf


def _valid_payload():
    return {
        "action": "save_flow",
        "name": "Order Summary",
        "description": "Queries open orders",
        "flow": {
            "version": 1,
            "nodes": [
                {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat Trigger", "config": {}}},
                {"id": "n2", "type": "output", "position": {"x": 200, "y": 0}, "data": {"label": "Output", "config": {}}},
            ],
            "edges": [{"id": "e1", "source": "n1", "sourceHandle": "source-main", "target": "n2", "targetHandle": "target-main"}],
        },
    }


def test_save_flow_assigns_missing_edge_ids(db):
    """The builder agent often omits edge ids — normalize them so canvas
    edits (delete/reconnect) have stable identities."""
    _mk_workflow(db)
    payload = {
        "action": "save_flow",
        "name": "Edge Ids",
        "flow": {
            "version": 1,
            "nodes": [
                {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
                {"id": "n2", "type": "output", "position": {"x": 200, "y": 0}, "data": {"label": "O", "config": {}}},
            ],
            "edges": [
                {"source": "n1", "sourceHandle": "source-main", "target": "n2", "targetHandle": "target-main"},
                {"source": "n1", "sourceHandle": "source-main", "target": "n2", "targetHandle": "target-main"},
            ],
        },
    }
    saved = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert saved["status"] == "flow_saved"
    ids = [e.get("id") for e in saved["flow"]["edges"]]
    assert ids == ["e1", "e2"]
    assert len(set(ids)) == 2


def test_save_flow_valid_persists_draft(db):
    _mk_workflow(db)
    payload = _valid_payload()
    saved = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert saved["status"] == "flow_saved"
    assert saved["flow"]["nodes"][0]["type"] == "trigger_chat"

    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row is not None
    assert row.name == "Order Summary"
    assert row.description == "Queries open orders"
    assert row.flow["nodes"][0]["type"] == "trigger_chat"


def test_save_flow_invalid_returns_errors(db):
    _mk_workflow(db)
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1, "nodes": [], "edges": []}}  # no trigger
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("trigger" in e.lower() for e in out["errors"])

    # Nothing persisted on invalid.
    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row.flow == {"version": 1, "nodes": [], "edges": []}


def test_save_flow_missing_nodes_shape_returns_errors(db):
    _mk_workflow(db)
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1}}
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("nodes" in e.lower() for e in out["errors"])


def _entity_ref_flow():
    return {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat", "config": {}}},
            {"id": "n2", "type": "agent", "position": {"x": 1, "y": 0}, "data": {"label": "Agent", "config": {"agent_id": "Order Agent"}}},
            {"id": "n3", "type": "tool", "position": {"x": 2, "y": 0}, "data": {"label": "Tool", "config": {"tool_id": "Web Search"}}},
            {"id": "n4", "type": "kb_search", "position": {"x": 3, "y": 0}, "data": {"label": "KB", "config": {"kb_id": "Products KB"}}},
            {"id": "n5", "type": "mcp_tool", "position": {"x": 4, "y": 0}, "data": {"label": "MCP", "config": {"server_id": "GitHub MCP", "tool_name": "list_repos"}}},
            {"id": "n6", "type": "output", "position": {"x": 5, "y": 0}, "data": {"label": "Out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n4"},
            {"id": "e4", "source": "n4", "target": "n5"},
            {"id": "e5", "source": "n5", "target": "n6"},
        ],
    }


def _seed_entities(db):
    db.add(AgentApp(id="agent-1", name="Order Agent", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.add(Tool(id="tool-1", name="Web Search", app_id="default-app",
                org_id="default-org", created_by_id="user-1"))
    db.add(KnowledgeBase(id="kb-1", name="Products KB", app_id="default-app",
                         org_id="default-org", created_by_id="user-1"))
    db.add(McpServer(id="srv-1", name="GitHub MCP", server_url="http://mcp.local",
                     app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.commit()


def test_resolve_entity_references_replaces_names_with_ids(db):
    _seed_entities(db)
    flow = _entity_ref_flow()
    notes = wfr.resolve_entity_references(flow, db)

    configs = {n["id"]: n["data"]["config"] for n in flow["nodes"]}
    assert configs["n2"]["agent_id"] == "agent-1"
    assert configs["n3"]["tool_id"] == "tool-1"
    assert configs["n4"]["kb_id"] == "kb-1"
    assert configs["n5"]["server_id"] == "srv-1"
    # Already-id values are left untouched (no note, no change).
    assert configs["n5"]["tool_name"] == "list_repos"
    assert any("n2" in note and "agent" in note for note in notes)


def test_resolve_entity_references_skips_uuid_ids_and_unknown(db):
    _seed_entities(db)
    flow = _entity_ref_flow()
    flow["nodes"][1]["data"]["config"]["agent_id"] = "11111111-2222-3333-4444-555555555555"
    flow["nodes"][2]["data"]["config"]["tool_id"] = "No Such Tool"
    wfr.resolve_entity_references(flow, db)
    configs = {n["id"]: n["data"]["config"] for n in flow["nodes"]}
    # UUID-ish id untouched; unknown name untouched (runtime reports at run time).
    assert configs["n2"]["agent_id"] == "11111111-2222-3333-4444-555555555555"
    assert configs["n3"]["tool_id"] == "No Such Tool"


def test_save_flow_ambiguous_entity_name_returns_flow_invalid(db):
    _mk_workflow(db)
    db.add(AgentApp(id="agent-a", name="Shared Name", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.add(AgentApp(id="agent-b", name="Shared Name", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.commit()

    flow = _entity_ref_flow()
    flow["nodes"][1]["data"]["config"]["agent_id"] = "Shared Name"
    payload = {"action": "save_flow", "name": "Ambiguous", "flow": flow}
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("ambigu" in e.lower() for e in out["errors"])


# ── Task 5: metadata.workflow_id passthrough at conversation creation ──────
# Pins the chain that feeds the save_flow branch (agents.py ~line 4711 reads
# ``(conv.metadata_ or {}).get("workflow_id")``): WorkflowBuilderChat.jsx calls
# ``createAgentConversation('workflow_builder', { workflow_id }, ...)``, which
# POSTs ``{agent_name, metadata: {workflow_id, ...}}`` to
# POST /apps/{app_id}/agents/conversations. create_conversation must persist
# that metadata so the workflow id reaches the save_flow branch.
def test_create_conversation_persists_workflow_id_metadata(db):
    conv = asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={
            "agent_name": "workflow_builder",
            "title": "Workflow builder",
            "metadata": {"workflow_id": "wf-1"},
        },
        db=db,
        user=_user(),
    ))

    # to_dict() serializes the metadata_ column under its DB name "metadata".
    assert conv["agent_name"] == "workflow_builder"
    assert conv["metadata"]["workflow_id"] == "wf-1"

    # The ORM row exposes it exactly the way the save_flow branch reads it.
    row = db.query(AgentConversation).filter(AgentConversation.id == conv["id"]).first()
    assert row is not None
    assert (row.metadata_ or {}).get("workflow_id") == "wf-1"


def test_create_conversation_metadata_workflow_id_survives_stale_project_id(db):
    """The project_id validation in create_conversation strips phantom project
    keys from metadata when the referenced project no longer exists — but must
    never strip unrelated keys like workflow_id."""
    conv = asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={
            "agent_name": "workflow_builder",
            "title": "Workflow builder",
            "metadata": {
                "workflow_id": "wf-1",
                "project_id": "00000000-0000-0000-0000-000000000000",  # phantom
                "project_name": "Ghost",
            },
        },
        db=db,
        user=_user(),
    ))

    assert conv["metadata"]["workflow_id"] == "wf-1"
    assert "project_id" not in conv["metadata"]
    assert "project_name" not in conv["metadata"]

    row = db.query(AgentConversation).filter(AgentConversation.id == conv["id"]).first()
    assert (row.metadata_ or {}).get("workflow_id") == "wf-1"


# ── Task 4 follow-ups: agents.py-side save_flow branch tests ─────────────────
# Exercise _persist_decision_summary_pause directly (router-direct pattern,
# same as the wfr-level tests above): fence + save_flow payload through the
# full branch, including the metadata flag, message persistence, repair-loop
# text, and the stale/foreign-workflow 404 conversion.


def _mk_wb_conv(db, wf_id: str | None = "wf-1", owner="user-1"):
    conv = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name="workflow_builder",
        title="Workflow builder",
        status="active",
        messages=[],
        metadata_={"workflow_id": wf_id},
        app_id="default-app",
        org_id="default-org",
        created_by_id=owner,
    )
    db.add(conv)
    db.commit()
    return conv


def _fence_text(payload, prose="Here is the workflow I built."):
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"{prose}\n\n:::decision-summary\n{body}\n:::"


def test_persist_decision_summary_pause_save_flow_success(db):
    _mk_workflow(db)
    conv = _mk_wb_conv(db)
    raw = _fence_text(_valid_payload())
    messages: list[dict] = []
    ok, stripped, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-1", [], raw, user=_user(),
    )
    assert ok is True
    assert note == "flow_saved"
    assert ":::decision-summary" not in stripped
    assert "Here is the workflow I built." in stripped
    # Metadata flag set + assistant message stored WITHOUT the fence.
    assert (conv.metadata_ or {}).get("workflow_flow_saved") is True
    assert len(messages) == 1
    assert messages[0]["id"] == "asm-1"
    assert ":::decision-summary" not in messages[0]["content"]
    # And the graph was actually persisted to the workflow row.
    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row.name == "Order Summary"
    assert row.flow["nodes"][0]["type"] == "trigger_chat"


def test_persist_decision_summary_pause_save_flow_invalid_repair_text(db):
    _mk_workflow(db)
    conv = _mk_wb_conv(db)
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1, "nodes": [], "edges": []}}
    raw = _fence_text(payload)
    messages: list[dict] = []
    ok, text, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-2", [], raw, user=_user(),
    )
    assert ok is False
    assert note == "flow_invalid"
    # Repair text carries the validation error for the LLM to fix next turn.
    assert "I couldn't save the workflow yet" in text
    assert "trigger" in text.lower()
    # Metadata flag NOT set, message NOT persisted, graph NOT overwritten.
    assert not (conv.metadata_ or {}).get("workflow_flow_saved")
    assert messages == []
    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row.flow == {"version": 1, "nodes": [], "edges": []}


def test_persist_decision_summary_pause_save_flow_missing_workflow_id(db):
    conv = _mk_wb_conv(db, wf_id=None)
    raw = _fence_text(_valid_payload())
    messages: list[dict] = []
    ok, text, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-3", [], raw, user=_user(),
    )
    assert ok is False
    assert note == "flow_save_failed"
    assert "no workflow attached" in text
    assert not (conv.metadata_ or {}).get("workflow_flow_saved")
    assert messages == []
    # Nothing persisted anywhere.
    assert db.query(AgentApp).count() == 0


def test_persist_decision_summary_pause_save_flow_stale_workflow(db):
    # No AgentApp row with id "wf-ghost" exists → save_workflow raises 404.
    # Fix: save_flow_from_decision_summary converts it to flow_save_failed —
    # no crash, no partial persist, explanation appended to the text.
    conv = _mk_wb_conv(db, wf_id="wf-ghost")
    raw = _fence_text(_valid_payload())
    messages: list[dict] = []
    ok, text, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-4", [], raw, user=_user(),
    )
    assert ok is False
    assert note == "flow_save_failed"
    assert "I couldn't save the workflow" in text
    assert "not found or not owned" in text
    assert not (conv.metadata_ or {}).get("workflow_flow_saved")
    assert messages == []


def test_persist_decision_summary_pause_save_flow_not_owned(db):
    # The workflow row exists but belongs to another user → 404 from
    # _get_owned_workflow → flow_save_failed, and the row is untouched.
    _mk_workflow(db, wf_id="wf-other", owner="user-2")
    conv = _mk_wb_conv(db, wf_id="wf-other")
    raw = _fence_text(_valid_payload())
    messages: list[dict] = []
    ok, text, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-5", [], raw, user=_user(),
    )
    assert ok is False
    assert note == "flow_save_failed"
    assert "not found or not owned" in text
    row = db.query(AgentApp).filter(AgentApp.id == "wf-other").first()
    assert row.flow == {"version": 1, "nodes": [], "edges": []}  # untouched


def test_persist_decision_summary_pause_save_flow_without_name_defaults(db):
    # Name-gate ordering: a save_flow payload WITHOUT a name must still reach
    # the branch (defaults to "Untitled Workflow") instead of being silently
    # dropped by the agent_builder name gate.
    _mk_workflow(db)
    conv = _mk_wb_conv(db)
    payload = _valid_payload()
    payload.pop("name")
    raw = _fence_text(payload)
    messages: list[dict] = []
    ok, stripped, note = agents._persist_decision_summary_pause(
        db, conv, messages, "asm-6", [], raw, user=_user(),
    )
    assert ok is True
    assert note == "flow_saved"
    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row.name == "Untitled Workflow"


def test_save_flow_stale_workflow_returns_flow_save_failed(db):
    # wfr-level: save_workflow raises HTTPException(404) for a missing row;
    # save_flow_from_decision_summary must convert it to a flow_save_failed
    # result instead of propagating the exception into the SSE stream.
    payload = _valid_payload()
    out = wfr.save_flow_from_decision_summary(payload, "wf-ghost", db, _user())
    assert out["status"] == "flow_save_failed"
    assert any(
        "not found" in e.lower() or "not owned" in e.lower()
        for e in out["errors"]
    )


def test_resolve_entity_references_scoped_to_owner(db):
    # user-2 owns the only "Order Agent"; user-1's flow must NOT resolve it —
    # the name stays untouched (consistent with unknown names), no ERROR note.
    db.add(AgentApp(id="agent-2", name="Order Agent", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-2"))
    db.commit()
    flow = _entity_ref_flow()
    notes = wfr.resolve_entity_references(flow, db, user=_user())
    configs = {n["id"]: n["data"]["config"] for n in flow["nodes"]}
    assert configs["n2"]["agent_id"] == "Order Agent"  # unresolved
    assert not any(n.startswith("ERROR: ") for n in notes)

    # An admin is NOT ownership-scoped — the name resolves to user-2's row.
    flow_admin = _entity_ref_flow()
    wfr.resolve_entity_references(flow_admin, db, user=_user(role="admin"))
    assert flow_admin["nodes"][1]["data"]["config"]["agent_id"] == "agent-2"

    # When the user owns an entity with the same name, the scoped lookup
    # resolves to THEIR row, not the other user's.
    db.add(AgentApp(id="agent-1", name="Order Agent", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.commit()
    flow_own = _entity_ref_flow()
    wfr.resolve_entity_references(flow_own, db, user=_user())
    assert flow_own["nodes"][1]["data"]["config"]["agent_id"] == "agent-1"


# ── Workflow session recognition: list_conversations workflow_id filter ─────
# The Build-with-AI panel resumes the latest session per workflow by querying
# GET /agents/conversations?agent_name=workflow_builder&workflow_id=<id>. The
# filter must be agent-scoped and backward-compatible (no workflow_id → all).

def test_list_conversations_filters_by_workflow_id(db):
    for wf in ("wf-1", "wf-2"):
        asyncio.run(agents.create_conversation(
            app_id="default-app",
            body={"agent_name": "workflow_builder", "title": "Builder",
                  "metadata": {"workflow_id": wf}},
            db=db, user=_user(),
        ))

    rows = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", workflow_id="wf-1",
        limit=None, skip=None, sort_by=None, db=db, user=_user(),
    ))
    assert len(rows) == 1
    assert rows[0]["metadata"]["workflow_id"] == "wf-1"

    # Backward compat: no workflow_id → both conversations.
    rows_all = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder",
        workflow_id=None, limit=None, skip=None, sort_by=None, db=db, user=_user(),
    ))
    assert len(rows_all) == 2


def test_list_conversations_workflow_id_filter_is_agent_scoped(db):
    """workflow_id alone must not leak other agents' conversations."""
    asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={"agent_name": "workflow_builder", "title": "B", "metadata": {"workflow_id": "wf-1"}},
        db=db, user=_user(),
    ))
    asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={"agent_name": "general_assistant", "title": "G", "metadata": {"workflow_id": "wf-1"}},
        db=db, user=_user(),
    ))
    rows = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", workflow_id="wf-1",
        limit=None, skip=None, sort_by=None, db=db, user=_user(),
    ))
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "workflow_builder"


def test_list_conversations_workflow_id_filter_returns_messages(db):
    """Resume needs the full conversation row — messages must be in to_dict."""
    conv = asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={"agent_name": "workflow_builder", "title": "B",
              "metadata": {"workflow_id": "wf-9"}},
        db=db, user=_user(),
    ))
    row = db.query(AgentConversation).filter(AgentConversation.id == conv["id"]).first()
    row.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    db.commit()

    rows = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", workflow_id="wf-9",
        limit=None, skip=None, sort_by=None, db=db, user=_user(),
    ))
    assert rows[0]["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]
