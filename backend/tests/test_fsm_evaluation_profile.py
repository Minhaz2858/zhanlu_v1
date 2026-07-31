"""Tests for evaluation_profile wiring into _run_verify (P2 Task 5)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from pathlib import Path


def test_fsm_run_verify_calls_verify_grounding():
    src = Path("/root/zhanlu/backend/app/services/synexia/fsm.py").read_text()
    assert "verify_grounding" in src
    assert "verify_grounding(self.execution, evaluation_profile)" in src


def test_fsm_loads_evaluation_profile_from_agent():
    src = Path("/root/zhanlu/backend/app/services/synexia/fsm.py").read_text()
    assert "def _load_evaluation_profile" in src
    assert "AgentApp" in src
    assert "evaluation_profile" in src
    assert "except Exception" in src


def test_fsm_run_verify_swallows_grounding_exceptions():
    src = Path("/root/zhanlu/backend/app/services/synexia/fsm.py").read_text()
    call_idx = src.find("verify_grounding(self.execution, evaluation_profile)")
    assert call_idx != -1
    pre = src[max(0, call_idx - 500):call_idx]
    assert "try:" in pre


def test_fsm_run_verify_persists_grounding_in_verification_payload():
    src = Path("/root/zhanlu/backend/app/services/synexia/fsm.py").read_text()
    assert 'factors["verification"] = result.to_dict()' in src


def test_fsm_load_evaluation_profile_returns_none_when_no_db():
    from app.services.synexia.fsm import SynexiaFSM
    fsm = SynexiaFSM.__new__(SynexiaFSM)
    fsm.db = None  # the helper's getattr guard returns None
    assert fsm._load_evaluation_profile(None) is None


class _FakeAgentAppBase:
    """Class-level attribute stubs so the helper's `AgentApp.id == ...` and
    `AgentApp.name == ...` don't raise AttributeError in tests."""
    id = "id-stub"
    name = "name-stub"


def test_fsm_load_evaluation_profile_uses_app_id_then_name(monkeypatch):
    from app.services.synexia.fsm import SynexiaFSM

    filter_calls = []

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            filter_calls.append((args, kwargs))
            return self
        def first(self):
            return None

    fsm = SynexiaFSM.__new__(SynexiaFSM)
    # The helper's first guard checks `getattr(self.db, "execute", None)` —
    # any non-None value passes. We use SimpleNamespace with execute set.
    fsm.db = SimpleNamespace(execute=lambda *a, **k: None, query=lambda c: _FakeQuery())
    monkeypatch.setitem(
        sys.modules,
        "app.models.agent_app",
        SimpleNamespace(AgentApp=_FakeAgentAppBase),
    )
    req = SimpleNamespace(app_id="app-real", agent_name="foo")
    fsm._load_evaluation_profile(req)
    # Two queries expected: app_id first, then agent_name fallback.
    assert len(filter_calls) >= 2


def test_fsm_load_evaluation_profile_returns_dict_when_present(monkeypatch):
    from app.services.synexia.fsm import SynexiaFSM

    class _FakeAgent:
        id = "app-x"
        name = "agent-x"
        evaluation_profile = {"grounding_checks": ["source_citation"]}

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self
        def first(self):
            return _FakeAgent()

    fsm = SynexiaFSM.__new__(SynexiaFSM)
    fsm.db = SimpleNamespace(execute=lambda *a, **k: None, query=lambda c: _FakeQuery())
    monkeypatch.setitem(
        sys.modules,
        "app.models.agent_app",
        SimpleNamespace(AgentApp=_FakeAgentAppBase),
    )
    req = SimpleNamespace(app_id="app-x", agent_name="agent-x")
    profile = fsm._load_evaluation_profile(req)
    assert profile == {"grounding_checks": ["source_citation"]}


def test_fsm_load_evaluation_profile_returns_none_when_profile_null(monkeypatch):
    from app.services.synexia.fsm import SynexiaFSM

    class _FakeAgent:
        id = "app-x"
        name = "agent-x"
        evaluation_profile = None

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self
        def first(self):
            return _FakeAgent()

    fsm = SynexiaFSM.__new__(SynexiaFSM)
    fsm.db = SimpleNamespace(execute=lambda *a, **k: None, query=lambda c: _FakeQuery())
    monkeypatch.setitem(
        sys.modules,
        "app.models.agent_app",
        SimpleNamespace(AgentApp=_FakeAgentAppBase),
    )
    req = SimpleNamespace(app_id="app-x", agent_name="agent-x")
    assert fsm._load_evaluation_profile(req) is None


def test_fsm_load_evaluation_profile_returns_none_on_db_exception(monkeypatch):
    from app.services.synexia.fsm import SynexiaFSM

    def _boom(_cls):
        raise RuntimeError("db down")
    fsm = SynexiaFSM.__new__(SynexiaFSM)
    fsm.db = SimpleNamespace(execute=lambda *a, **k: None, query=_boom)
    monkeypatch.setitem(
        sys.modules,
        "app.models.agent_app",
        SimpleNamespace(AgentApp=_FakeAgentAppBase),
    )
    req = SimpleNamespace(app_id="app-x", agent_name="agent-x")
    assert fsm._load_evaluation_profile(req) is None
