"""Tests for the swarm runtime (mailbox + handoff + role registry)."""

import time
import pytest

from app.services.swarm import (
    Handoff,
    HandoffProtocol,
    Mailbox,
    RoleSpec,
    get_role,
    list_roles,
    register_role,
    spawn_subagent,
)


def test_handoff_round_trip_json():
    h = Handoff(
        from_role="main",
        to_role="researcher",
        payload={"q": "what is X"},
    )
    raw = h.to_json()
    h2 = Handoff.from_json(raw)
    assert h2.from_role == "main"
    assert h2.to_role == "researcher"
    assert h2.payload == {"q": "what is X"}
    assert h2.id == h.id


def test_mailbox_inproc_push_pop_drains():
    """Without Redis the in-process queue is used; push/pop is FIFO."""
    m = Mailbox("agent-test-1", redis_client=None)
    h1 = Handoff(from_role="a", to_role="b", payload={"i": 1})
    h2 = Handoff(from_role="a", to_role="b", payload={"i": 2})
    assert m.push(h1) is True
    assert m.push(h2) is True
    assert m.size() >= 1
    popped = m.drain()
    payloads = [h.payload["i"] for h in popped]
    assert 1 in payloads and 2 in payloads


def test_handoff_protocol_routes_to_mailbox():
    proto = HandoffProtocol(redis_client=None)
    h = proto.send(
        from_role="main",
        to_role="researcher",
        to_agent_id="agent-route-test",
        payload={"q": "hi"},
    )
    assert h is not None
    assert h.to_role == "researcher"
    m = Mailbox("agent-route-test", redis_client=None)
    # The handoff should be in the mailbox.
    drained = m.drain()
    assert any(x.id == h.id for x in drained)


def test_handoff_protocol_rejects_missing_targets():
    proto = HandoffProtocol(redis_client=None)
    assert proto.send(from_role="a", to_role="", to_agent_id="x", payload={}) is None
    assert proto.send(from_role="a", to_role="b", to_agent_id="", payload={}) is None


def test_role_registry_builtin_roles_present():
    names = {r.name for r in list_roles()}
    assert {"researcher", "coder", "critic", "writer"}.issubset(names)


def test_role_registry_get_role_returns_spec():
    r = get_role("coder")
    assert r is not None
    assert "run_sandbox_skill" in r.allowed_tools


def test_register_role_extends_registry():
    custom = RoleSpec(
        name="custom_tester",
        description="tester role",
        system_prompt="You run tests.",
        allowed_tools=["code_execution"],
    )
    register_role(custom)
    assert get_role("custom_tester") is custom


def test_spawn_subagent_returns_id_for_known_role():
    sid = spawn_subagent("researcher", parent_agent_id="main")
    assert sid is not None
    assert sid.startswith("main:researcher:")


def test_spawn_subagent_returns_none_for_unknown_role():
    assert spawn_subagent("nonexistent") is None
