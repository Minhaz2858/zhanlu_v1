"""Test that SynexiaFSM is enabled by default (Task 4 of P1 plan).

Rolling back: set env var SYNEXIA_FSM_ENABLED=false to revert to the raw
tool loop without rebuilding.
"""

from __future__ import annotations

import os
import importlib

from app.config import Settings, settings


def test_fsm_enabled_by_default_in_settings_class():
    """The Settings class default must be True so new deployments get the FSM."""
    fresh = Settings()
    assert fresh.SYNEXIA_FSM_ENABLED is True


def test_fsm_flag_can_be_disabled_via_env(monkeypatch):
    """Rollback path: setting SYNEXIA_FSM_ENABLED=false reverts to the raw loop."""
    monkeypatch.setenv("SYNEXIA_FSM_ENABLED", "false")
    fresh = Settings()
    assert fresh.SYNEXIA_FSM_ENABLED is False


def test_runtime_is_fsm_enabled_returns_true_in_default_env():
    """The runtime is_fsm_enabled() helper must reflect the new default."""
    from app.services.synexia.fsm import is_fsm_enabled
    # In the test env, no env var is set — default should be True now.
    assert is_fsm_enabled() is True


def test_comment_documents_rollback_path():
    """The config.py comment must mention the rollback path."""
    from pathlib import Path
    src = Path("/root/zhanlu/backend/app/config.py").read_text()
    assert "SYNEXIA_FSM_ENABLED" in src
    # Look for a rollback hint near the flag.
    idx = src.find("SYNEXIA_FSM_ENABLED")
    block = src[idx - 200: idx + 400]
    assert "rollback" in block.lower() or "revert" in block.lower(), (
        "Config comment should document the rollback path (env SYNEXIA_FSM_ENABLED=false)"
    )
