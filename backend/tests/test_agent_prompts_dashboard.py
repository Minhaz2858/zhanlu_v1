import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_prompts import _build_default_skills_block


def test_default_skills_block_describes_live_dashboard_workflow():
    block = _build_default_skills_block()

    assert "live database-backed dashboard" in block
    assert "create_dashboard" in block
    assert "update_dashboard" in block
    assert "uiux_design_system" in block
    assert "Dashboard is NOT part of the generic create_artifact file path" in block
    assert "Do not call `ask_data_agent` for dashboard creation" in block
    assert "call `create_dashboard` before the tool loop budget is exhausted" in block
