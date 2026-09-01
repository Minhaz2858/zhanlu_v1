import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_prompts import _build_default_skills_block


def test_default_skills_block_describes_fullstack_dashboard_workflow():
    block = _build_default_skills_block()

    assert "live updates — NEVER a static page, NEVER fabricated data" in block
    assert "create_fullstack_dashboard" in block
    assert "update_fullstack_dashboard" in block
    assert "uiux_design_system" in block
    assert "DATA-CONTRACT CONFIRMATION (HARD RULE" in block
    assert "create_artifact" in block
