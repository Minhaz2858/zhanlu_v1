"""fusion360_dfm_check tool — roadmap P1. Thin validator + delegator to the
pure DFM rule engine; registered in the cad toolset so the agent can run
manufacturability checks after a PASS verify_build.
"""
import json

import pytest

from app.services.tool_handlers import fusion360_dfm as fd


def test_process_required():
    out = fd._fusion360_dfm_check({"measurements": {}}, None, "u")
    assert not out["success"] and "process" in out["error"]


def test_measurements_required():
    out = fd._fusion360_dfm_check({"process": "cnc"}, None, "u")
    assert not out["success"] and "measurements" in out["error"]


def test_measurements_json_string_accepted():
    out = fd._fusion360_dfm_check(
        {"process": "cnc", "measurements": json.dumps({"wall_thickness_mm": 0.4})},
        None, "u")
    assert out["success"] is True
    assert out["dfm"]["verdict"] == "fail"


def test_unknown_process_is_retryable_error():
    out = fd._fusion360_dfm_check(
        {"process": "woodcarving", "measurements": {}}, None, "u")
    assert not out["success"] and "cnc" in out["error"]


def test_fail_verdict_surfaced_in_message():
    out = fd._fusion360_dfm_check(
        {"process": "cnc", "material": "6061-t6",
         "measurements": {"wall_thickness_mm": 0.4}}, None, "u")
    assert out["success"] is True
    assert out["dfm"]["verdict"] == "fail"
    assert "1 error" in out["message"]


def test_pass_verdict_surfaced():
    out = fd._fusion360_dfm_check(
        {"process": "cnc", "material": "6061-t6",
         "measurements": {"wall_thickness_mm": 3.0}}, None, "u")
    assert out["dfm"]["verdict"] == "pass"
    assert "PASS" in out["message"]


def test_registered_in_tool_registry():
    import app.services.tool_handlers.fusion360_dfm  # noqa: F401 (registration)
    from app.services.tool_registry import registry
    assert "fusion360_dfm_check" in registry.list_names()
    entry = registry.get_entry("fusion360_dfm_check")
    assert entry is not None and entry.handler is fd._fusion360_dfm_check
