"""Fusion 360 bridge — actionable sketch-index pre-flight guard (2026-11-02).

Root cause of the "M8x40 hex bolt built nothing" incident: on a brand-new
EMPTY document (0 sketches — fusion360_clear had just reported
`CLEARED bodies: 0`), the model called fusion360_sketch_polygon with a
sketch_index that didn't exist → `RuntimeError: 3 : invalid argument index`
inside the Fusion add-in → 2 attempts exhausted → the model returned an
EMPTY completion → the turn ended with the honest fallback text and an
empty Fusion window.

Fix: index-based sketch tools now emit an ACTIONABLE pre-flight error
(NO_SKETCH_ERROR → call fusion360_sketch_create first) that the model can
self-correct from, instead of a cryptic crash.
"""
import pytest

from app.services.tool_handlers import fusion360_granular as g


@pytest.fixture
def capture(monkeypatch):
    """Capture the generated code instead of hitting the bridge."""
    seen = {}

    def _fake_run(code, db=None):
        seen["code"] = code
        return {"success": True, "stdout": "OK"}

    monkeypatch.setattr(g, "_run", _fake_run)
    return seen


def test_guard_content_is_actionable():
    code = g._sketch_guard_code(1)
    assert "NO_SKETCH_ERROR" in code
    assert "fusion360_sketch_create" in code  # tells the model HOW to recover
    assert "root.sketches.count == 0" in code
    assert "root.sketches.count <= 1" in code


def test_polygon_guard_runs_before_index_access(capture):
    g._fusion360_sketch_polygon(
        {"sketch_index": 1, "sides": 6, "circumradius_mm": 5}, None, "u1",
    )
    code = capture["code"]
    assert "NO_SKETCH_ERROR" in code
    assert code.index("NO_SKETCH_ERROR") < code.index("root.sketches.item(1)")
    assert "OK polygon" in code


def test_circle_guard_runs_before_index_access(capture):
    g._fusion360_sketch_circle(
        {"sketch_index": 2, "radius_mm": 4, "cx_mm": 0, "cy_mm": 0}, None, "u1",
    )
    code = capture["code"]
    assert code.index("root.sketches.count <= 2") < code.index("root.sketches.item(2)")


def test_rectangle_guard_runs_before_index_access(capture):
    g._fusion360_sketch_rectangle(
        {"sketch_index": 0, "x1_mm": 0, "y1_mm": 0, "x2_mm": 10, "y2_mm": 10},
        None, "u1",
    )
    code = capture["code"]
    assert "NO_SKETCH_ERROR" in code
    assert "addTwoPointRectangle" in code


def test_extrude_guard_includes_sketch_and_profile_checks(capture):
    g._fusion360_extrude(
        {"sketch_index": 3, "distance_mm": 10, "direction": "pos",
         "operation": "new", "profile_index": 0},
        None, "u1",
    )
    code = capture["code"]
    assert "NO_SKETCH_ERROR" in code
    assert "NO_PROFILE_ERROR" in code
    assert code.index("NO_SKETCH_ERROR") < code.index("root.sketches.item(3)")


def test_missing_sketch_index_still_rejected_without_bridge_call(capture, monkeypatch):
    """The guard must never run generated code when sketch_index is absent."""
    def _boom(*a, **k):
        raise AssertionError("bridge must not be called")
    monkeypatch.setattr(g, "_run", _boom)
    r = g._fusion360_sketch_polygon({"sides": 6}, None, "u1")
    assert r["success"] is False
    assert "sketch_index is required" in r["error"]


def test_guard_code_is_syntactically_valid_python():
    compile(g._sketch_guard_code(0) + "sk = root.sketches.item(0)\n", "<guard>", "exec")
