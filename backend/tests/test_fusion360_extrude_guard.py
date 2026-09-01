from app.services.tool_handlers.fusion360_granular import _extrude_guard_code


def test_extrude_guard_code_checks_profiles_before_extrude():
    code = _extrude_guard_code(0)
    assert "profiles.count" in code
    assert "NO_PROFILE_ERROR" in code
    # when composed ahead of the extrude call, the guard fires before createInput
    composed = code + "e = root.features.extrudeFeatures.createInput(sk.profiles.item(0), 0)\n"
    assert composed.index("profiles.count") < composed.index("createInput")
