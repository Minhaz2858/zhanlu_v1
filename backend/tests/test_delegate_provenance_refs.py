"""Phase 8 T8-6 — delegation provenance linkage (authority never transfers).

``provenance_parent_refs`` extracts ONLY identity/trace keys from the
parent's tool context so the child execution can be traced to its parent
in the provenance envelope. Authorization-relevant keys (bindings,
permissions, tool config, agent identity override) are deliberately
excluded — the child is permission-checked as itself.
"""

from app.services.tool_handlers.delegate_tool import provenance_parent_refs


def test_parent_refs_from_context():
    refs = provenance_parent_refs({
        "agent_name": "parent-agent", "agent_app_id": "app-p",
        "run_id": "run-1", "conversation_id": "c1"})
    assert refs == {"parent_agent_name": "parent-agent",
                    "parent_agent_app_id": "app-p", "parent_run_id": "run-1"}


def test_parent_refs_absent_when_missing():
    assert provenance_parent_refs({}) == {}
    assert provenance_parent_refs(None) == {}


def test_parent_refs_never_include_permissions():
    refs = provenance_parent_refs({
        "agent_name": "p", "data_read": True, "tool_config": {"x": 1},
        "bound_kb_ids": ["kb-1"]})
    assert "bound_kb_ids" not in refs and "data_read" not in refs \
        and "tool_config" not in refs
