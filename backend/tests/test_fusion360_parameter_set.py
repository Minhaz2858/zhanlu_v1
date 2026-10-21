"""fusion360_parameter_set — dedicated parametric UPDATE tool (P0).

``fusion360_user_parameter`` is create-or-update; ``fusion360_parameter_set``
is UPDATE-ONLY with a parsed mm read-back. Pins the roadmap P0 foundation
(docs/superpowers/specs/2026-10-19-cad-agent-operational-roadmap.md):
parameters must be changeable by name and re-verified, so engineering-change
propagation (P3) and the RFQ rebuild loop (P2) have a primitive to build on.
"""
import pytest

from app.services.tool_handlers import fusion360_parametric as fp


@pytest.fixture
def bridge(monkeypatch):
    """Capture the snippet and fake the bridge response per test."""
    calls = {}

    def _fake_run(code, db):
        calls["code"] = code
        return calls.get("response", {"success": True, "stdout": ""})

    monkeypatch.setattr(fp, "_run", _fake_run)
    return calls


def _ok_stdout(name="c1a2b3c4_thk", raw_cm="0.4"):
    return f"PARAM_SET_OK {name}={raw_cm}"


def test_rejects_missing_name(bridge):
    out = fp._fusion360_parameter_set({"value_mm": 4}, None, "u")
    assert not out["success"] and "name" in out["error"]


def test_rejects_value_and_expression_together(bridge):
    out = fp._fusion360_parameter_set(
        {"name": "p", "value_mm": 4, "expression": "4 mm"}, None, "u")
    assert not out["success"] and "exactly one" in out["error"]


def test_rejects_neither_value_nor_expression(bridge):
    out = fp._fusion360_parameter_set({"name": "p"}, None, "u")
    assert not out["success"] and "exactly one" in out["error"]


def test_rejects_non_numeric_value_mm(bridge):
    out = fp._fusion360_parameter_set({"name": "p", "value_mm": "abc"}, None, "u")
    assert not out["success"] and "value_mm" in out["error"]


def test_update_by_value_mm_parses_readback(bridge):
    bridge["response"] = {"success": True, "stdout": _ok_stdout(raw_cm="0.4")}
    out = fp._fusion360_parameter_set(
        {"name": "c1a2b3c4_thk", "value_mm": 4}, None, "u")
    assert out["success"] is True
    assert out["value_mm"] == 4.0
    assert "p.expression = '4 mm'" in bridge["code"]
    assert "itemByName(" in bridge["code"]


def test_update_by_expression_passes_through(bridge):
    bridge["response"] = {"success": True, "stdout": _ok_stdout()}
    out = fp._fusion360_parameter_set(
        {"name": "p", "expression": "40 + stroke_pos * 110"}, None, "u")
    assert out["success"] is True
    assert "'40 + stroke_pos * 110'" in bridge["code"]


def test_not_found_is_error_with_params_list_hint(bridge):
    bridge["response"] = {"success": True, "stdout": "PARAM_SET_ERR not_found"}
    out = fp._fusion360_parameter_set({"name": "nope", "value_mm": 4}, None, "u")
    assert not out["success"] and "fusion360_params_list" in out["error"]


def test_bridge_failure_propagates(bridge):
    bridge["response"] = {"success": False, "error": "bridge offline"}
    out = fp._fusion360_parameter_set({"name": "p", "value_mm": 4}, None, "u")
    assert out["success"] is False and "bridge offline" in out["error"]


def test_name_quote_rejected(bridge):
    out = fp._fusion360_parameter_set({"name": "p'x", "value_mm": 4}, None, "u")
    assert not out["success"]


def test_registered_in_tool_registry():
    from app.services.tool_registry import registry
    assert "fusion360_parameter_set" in registry.list_names()
    entry = registry.get_entry("fusion360_parameter_set")
    assert entry is not None and entry.handler is fp._fusion360_parameter_set
