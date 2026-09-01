from app.services.tool_handlers.fusion360_sketch_common import (
    _CLOSE_SKETCH, _profile_report,
)


def test_closure_snippet_has_constraints_and_report():
    assert "addCoincident" in _CLOSE_SKETCH
    assert "PROFILES" in _CLOSE_SKETCH
    assert "WARN_NO_PROFILE" in _CLOSE_SKETCH


def test_profile_report_line():
    assert _profile_report(0).startswith("print('WARN_NO_PROFILE'")
    assert _profile_report(1).startswith("print('PROFILES 1'")
