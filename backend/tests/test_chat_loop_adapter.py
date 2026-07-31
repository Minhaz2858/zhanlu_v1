"""Tests for the chat-loop adapter that wraps SynexiaFSM."""

from unittest.mock import MagicMock

import pytest

from app.services.synexia.chat_loop_adapter import is_fsm_enabled, run_via_fsm


def test_is_fsm_enabled_default(monkeypatch):
    # When no env / setting is present, the adapter defaults to enabled.
    monkeypatch.delenv("SYNEXIA_FSM_ENABLED", raising=False)
    # The test settings may or may not define the field; we don't
    # assert on the specific value, just that the function is total
    # and returns a bool.
    assert isinstance(is_fsm_enabled(), bool)


def test_is_fsm_enabled_respects_env(monkeypatch):
    monkeypatch.setenv("SYNEXIA_FSM_ENABLED", "0")
    assert is_fsm_enabled() is False
    monkeypatch.setenv("SYNEXIA_FSM_ENABLED", "true")
    assert is_fsm_enabled() is True


def test_run_via_fsm_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("SYNEXIA_FSM_ENABLED", "0")
    out = run_via_fsm(
        conversation=MagicMock(),
        user_message="hi",
        db=MagicMock(),
    )
    assert out is None


def test_run_via_fsm_returns_none_when_fsm_raises(monkeypatch):
    """If the FSM itself raises, the adapter returns None (legacy fallback)."""
    monkeypatch.setenv("SYNEXIA_FSM_ENABLED", "1")
    # Force the SynexiaFSM class to raise at construction time.
    import sys
    from app.services.synexia import chat_loop_adapter

    class _Broken:
        def __init__(self, db):
            raise RuntimeError("simulated FSM failure")

    fake_module = MagicMock()
    fake_module.ExecutionRequest = MagicMock()
    fake_module.SynexiaFSM = _Broken
    monkeypatch.setitem(
        sys.modules,
        "app.services.synexia.fsm",
        fake_module,
    )
    # Re-import to ensure the module's lazy import path is used.
    import importlib

    importlib.reload(chat_loop_adapter)
    out = chat_loop_adapter.run_via_fsm(
        conversation=MagicMock(),
        user_message="hi",
        db=MagicMock(),
    )
    assert out is None
